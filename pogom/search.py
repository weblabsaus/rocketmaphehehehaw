#!/usr/bin/python
# -*- coding: utf-8 -*-

'''
Search Architecture:
 - Have a list of accounts
 - Create an "overseer" thread
 - Search Overseer:
   - Tracks incoming new location values
   - Tracks "paused state"
   - During pause or new location will clears current search queue
   - Starts search_worker threads
 - Search Worker Threads each:
   - Have a unique API login
   - Listens to the same Queue for areas to scan
   - Can re-login as needed
   - Pushes finds to db queue and webhook queue
'''

import logging
import math
import os
import sys
import traceback
import random
import time
import geopy
import geopy.distance
import requests

from datetime import datetime, timedelta
from threading import Thread, Lock
from queue import Queue, Empty

from pgoapi import PGoApi
from pgoapi.utilities import f2i
from pgoapi import utilities as util
from pgoapi.exceptions import AuthException

from .models import parse_map, GymDetails, parse_gyms, MainWorker, WorkerStatus, Token
from .fakePogoApi import FakePogoApi
from .utils import now
from .transform import get_new_coords
import schedulers

from .proxy import get_new_proxy

import terminalsize

log = logging.getLogger(__name__)

TIMESTAMP = '\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000'

loginDelayLock = Lock()

token_needed = 0

tokenLock = Lock()


# Apply a location jitter.
def jitterLocation(location=None, maxMeters=10):
    origin = geopy.Point(location[0], location[1])
    b = random.randint(0, 360)
    d = math.sqrt(random.random()) * (float(maxMeters) / 1000)
    destination = geopy.distance.distance(kilometers=d).destination(origin, b)
    return (destination.latitude, destination.longitude, location[2])


# Thread to handle user input.
def switch_status_printer(args, display_type, current_page, pause_bit):
    # Get a reference to the root logger.
    mainlog = logging.getLogger()
    # Disable logging of the first handler - the stream handler, and disable it's output.
    mainlog.handlers[0].setLevel(logging.CRITICAL)

    while True:
        # Wait for the user to press a key.
        command = raw_input()

        if command == '':
            # Switch between logging and display.
            if display_type[0] != 'logs':
                # Disable display, enable on screen logging.
                mainlog.handlers[0].setLevel(logging.DEBUG)
                display_type[0] = 'logs'
                # If logs are going slowly, sometimes it's hard to tell you switched.  Make it clear.
                print 'Showing logs...'
            elif display_type[0] == 'logs':
                # Enable display, disable on screen logging (except for critical messages).
                mainlog.handlers[0].setLevel(logging.CRITICAL)
                display_type[0] = 'workers'
        elif command.isdigit():
            current_page[0] = int(command)
            mainlog.handlers[0].setLevel(logging.CRITICAL)
            display_type[0] = 'workers'
        elif command.lower() == 'f':
            mainlog.handlers[0].setLevel(logging.CRITICAL)
            display_type[0] = 'failedaccounts'
        elif command.lower() == 'c':
            if args.captcha_solving_strategy == 'manual-first':
                args.captcha_solving_strategy = 'manual-only'
            elif args.captcha_solving_strategy == 'manual-only':
                args.captcha_solving_strategy = 'automatic'
            elif args.captcha_solving_strategy == 'automatic':
                args.captcha_solving_strategy = 'manual-first'
            log.info("Captcha solving strategy changed to '{}'".format(args.captcha_solving_strategy))
        elif command.lower() == 'p':
            if pause_bit.is_set():
                pause_bit.clear()
                log.info('Scanning resumed.')
            else:
                pause_bit.set()
                log.info('Scanning paused.')


# Thread to print out the status of each worker.
def status_printer(args, threadStatus, search_items_queue_array, db_updates_queue, wh_queue, account_queue,
                   account_failures, pause_bit):
    display_type = ["workers"]
    current_page = [1]

    # Start another thread to get user input.
    t = Thread(target=switch_status_printer,
               name='switch_status_printer',
               args=(args, display_type, current_page, pause_bit))
    t.daemon = True
    t.start()

    while True:
        time.sleep(1)

        if display_type[0] == 'logs':
            # In log display mode, we don't want to show anything.
            continue

        # Create a list to hold all the status lines, so they can be printed all at once to reduce flicker.
        status_text = []

        if display_type[0] == 'workers':

            # Get the terminal size.
            width, height = terminalsize.get_terminal_size()
            # Queue and overseer take 2 lines.  Switch message takes up 2 lines.  Remove an extra 2 for things like screen status lines.
            usable_height = height - 6
            # Prevent people running terminals only 6 lines high from getting a divide by zero.
            if usable_height < 1:
                usable_height = 1

            # Calculate total skipped items.
            skip_total = 0
            for item in threadStatus:
                if 'skip' in threadStatus[item]:
                    skip_total += threadStatus[item]['skip']

            # Print the queue length.
            search_items_queue_size = 0
            for i in range(0, len(search_items_queue_array)):
                search_items_queue_size += search_items_queue_array[i].qsize()

            status_text.append('Queues: {} search items, {} db updates, {} webhook.  Total skipped items: {}. Spare accounts available: {}. Accounts on hold: {}. Token needed: {}'
                               .format(search_items_queue_size, db_updates_queue.qsize(), wh_queue.qsize(), skip_total, account_queue.qsize(), len(account_failures), token_needed))

            # Print status of overseer.
            status_text.append('{} Overseer: {}'.format(threadStatus['Overseer']['scheduler'], threadStatus['Overseer']['message']))

            # Calculate the total number of pages.  Subtracting for the overseer.
            total_pages = math.ceil((len(threadStatus) - 1 - threadStatus['Overseer']['message'].count('\n')) /
                                    float(usable_height))

            # Prevent moving outside the valid range of pages.
            if current_page[0] > total_pages:
                current_page[0] = total_pages
            if current_page[0] < 1:
                current_page[0] = 1

            # Calculate which lines to print.
            start_line = usable_height * (current_page[0] - 1)
            end_line = start_line + usable_height
            current_line = 1

            # Find the longest username and proxy.
            userlen = 4
            proxylen = 5
            for item in threadStatus:
                if threadStatus[item]['type'] == 'Worker':
                    userlen = max(userlen, len(threadStatus[item]['username']))
                    if 'proxy_display' in threadStatus[item]:
                        proxylen = max(proxylen, len(str(threadStatus[item]['proxy_display'])))

            # How pretty.
            status = '{:10} | {:5} | {:' + str(userlen) + '} | {:' + str(proxylen) + '} | {:7} | {:6} | {:5} | {:7} | {:8} | {:10}'

            # Print the worker status.
            status_text.append(status.format('Worker ID', 'Start', 'User', 'Proxy', 'Success', 'Failed', 'Empty', 'Skipped', 'Captchas', 'Message'))

            # set our hour variables.
            countuser = 0
            countsuccess = 0
            countfailed = 0
            countempty = 0
            countskipped = 0
            countcaptchas = 0

            for item in sorted(threadStatus):
                if(threadStatus[item]['type'] == 'Worker'):
                    # Count our hour variables
                    countuser += 1
                    countsuccess += threadStatus[item]['success']
                    countfailed += threadStatus[item]['fail']
                    countempty += threadStatus[item]['noitems']
                    countskipped += threadStatus[item]['skip']
                    countcaptchas += threadStatus[item]['captchas']
                    if countuser <= 1:
                        elapsed = now() - threadStatus[item]['starttime']
                    current_line += 1

                    # Skip over items that don't belong on this page.
                    if current_line < start_line:
                        continue
                    if current_line < end_line:
                        status_text.append(status.format(item, time.strftime('%H:%M', time.localtime(threadStatus[item]['starttime'])), threadStatus[item]['username'], threadStatus[item]['proxy_display'], threadStatus[item]['success'], threadStatus[item]['fail'], threadStatus[item]['noitems'], threadStatus[item]['skip'], threadStatus[item]['captchas'], threadStatus[item]['message']))

        elif display_type[0] == 'failedaccounts':
            status_text.append('-----------------------------------------')
            status_text.append('Accounts on hold:')
            status_text.append('-----------------------------------------')

            # Find the longest account name.
            userlen = 4
            for account in account_failures:
                userlen = max(userlen, len(account['account']['username']))

            status = '{:' + str(userlen) + '} | {:10} | {:20}'
            status_text.append(status.format('User', 'Hold Time', 'Reason'))

            for account in account_failures:
                status_text.append(status.format(account['account']['username'], time.strftime('%H:%M:%S', time.localtime(account['last_fail_time'])), account['reason']))

        # Print the status_text for the current screen.
        # determine hourly rates
        sph = countsuccess * 3600 / elapsed
        fph = countfailed * 3600 / elapsed
        eph = countempty * 3600 / elapsed
        sph = countskipped * 3600 / elapsed
        cph = countcaptchas * 3600 / elapsed
        ccost = cph * 0.003
        cmonth = ccost * 730
        status_text.append('Total active: {}  |  Success: {} ({}/hr) | Fails: {} ({}/hr) | Empties: {} ({}/hr) | Skips {} ({}/hr) | Captchas: {} ({}/hr)|${:2}/hr|${:2}/mo'.format(countuser, countsuccess, sph, countfailed, fph, countempty, eph, countskipped, sph, countcaptchas, cph, ccost, cmonth))
        status_text.append('Page {}/{}. Page number to switch pages. F to show on hold accounts. <ENTER> alone to switch between status and log view'.format(current_page[0], total_pages))
        # Clear the screen.
        os.system('cls' if os.name == 'nt' else 'clear')
        # Print status.
        print "\n".join(status_text)


# The account recycler monitors failed accounts and places them back in the account queue 2 hours after they failed.
# This allows accounts that were soft banned to be retried after giving them a chance to cool down.
def account_recycler(accounts_queue, account_failures, args):
    while True:
        # Run once a minute.
        time.sleep(60)
        log.info('Account recycler running. Checking status of {} accounts'.format(len(account_failures)))

        # Create a new copy of the failure list to search through, so we can iterate through it without it changing.
        failed_temp = list(account_failures)

        # Search through the list for any item that last failed before -ari/--account-rest-interval seconds
        ok_time = now() - args.account_rest_interval
        for a in failed_temp:
            if a['last_fail_time'] <= ok_time:
                # Remove the account from the real list, and add to the account queue.
                log.info('Account {} returning to active duty.'.format(a['account']['username']))
                account_failures.remove(a)
                accounts_queue.put(a['account'])
            else:
                if 'notified' not in a:
                    log.info('Account {} needs to cool off for {} minutes due to {}'.format(a['account']['username'], round((a['last_fail_time'] - ok_time) / 60, 0), a['reason']))
                    a['notified'] = True


def worker_status_db_thread(threads_status, name, db_updates_queue):

    while True:
        workers = {}
        overseer = None
        for status in threads_status.values():
            if status['type'] == 'Overseer':
                overseer = {
                    'worker_name': name,
                    'message': status['message'],
                    'method': status['scheduler'],
                    'last_modified': datetime.utcnow()
                }
            elif status['type'] == 'Worker':
                workers[status['username']] = WorkerStatus.db_format(status, name)
        if overseer is not None:
            db_updates_queue.put((MainWorker, {0: overseer}))
            db_updates_queue.put((WorkerStatus, workers))
        time.sleep(3)


# The main search loop that keeps an eye on the over all process.
def search_overseer_thread(args, new_location_queue, pause_bit, heartb, db_updates_queue, wh_queue):

    log.info('Search overseer starting')

    search_items_queue_array = []
    scheduler_array = []
    account_queue = Queue()
    threadStatus = {}

    '''
    Create a queue of accounts for workers to pull from. When a worker has failed too many times,
    it can get a new account from the queue and reinitialize the API. Workers should return accounts
    to the queue so they can be tried again later, but must wait a bit before doing do so to
    prevent accounts from being cycled through too quickly.
    '''
    for i, account in enumerate(args.accounts):
        account_queue.put(account)

    # Create a list for failed accounts.
    account_failures = []

    threadStatus['Overseer'] = {
        'message': 'Initializing',
        'type': 'Overseer',
        'scheduler': args.scheduler
    }

    if(args.print_status):
        log.info('Starting status printer thread')
        t = Thread(target=status_printer,
                   name='status_printer',
                   args=(args, threadStatus, search_items_queue_array, db_updates_queue, wh_queue, account_queue,
                         account_failures, pause_bit))
        t.daemon = True
        t.start()

    # Create account recycler thread.
    log.info('Starting account recycler thread')
    t = Thread(target=account_recycler, name='account-recycler', args=(account_queue, account_failures, args))
    t.daemon = True
    t.start()

    if args.status_name is not None:
        log.info('Starting status database thread')
        t = Thread(target=worker_status_db_thread,
                   name='status_worker_db',
                   args=(threadStatus, args.status_name, db_updates_queue))
        t.daemon = True
        t.start()

    # Create specified number of search_worker_thread.
    log.info('Starting search worker threads')
    for i in range(0, args.workers):
        log.debug('Starting search worker thread %d', i)

        if i == 0 or (args.beehive and i % args.workers_per_hive == 0):
            search_items_queue = Queue()
            # Create the appropriate type of scheduler to handle the search queue.
            scheduler = schedulers.SchedulerFactory.get_scheduler(args.scheduler, [search_items_queue], threadStatus, args)

            scheduler_array.append(scheduler)
            search_items_queue_array.append(search_items_queue)

        # Set proxy for each worker, using round robin.
        proxy_display = 'No'
        proxy_url = False    # Will be assigned inside a search thread

        workerId = 'Worker {:03}'.format(i)
        threadStatus[workerId] = {
            'type': 'Worker',
            'message': 'Creating thread...',
            'success': 0,
            'fail': 0,
            'noitems': 0,
            'skip': 0,
            'captchas': 0,
            'username': '',
            'proxy_display': proxy_display,
            'proxy_url': proxy_url
        }

        t = Thread(target=search_worker_thread,
                   name='search-worker-{}'.format(i),
                   args=(args, account_queue, account_failures, search_items_queue, pause_bit,
                         threadStatus[workerId],
                         db_updates_queue, wh_queue, scheduler))
        t.daemon = True
        t.start()

    # A place to track the current location.
    current_location = False

    # The real work starts here but will halt on pause_bit.set().
    while True:

        if args.on_demand_timeout > 0 and (now() - args.on_demand_timeout) > heartb[0]:
            pause_bit.set()
            log.info("Searching paused due to inactivity...")

        # Wait here while scanning is paused.
        while pause_bit.is_set():
            for i in range(0, len(scheduler_array)):
                scheduler_array[i].scanning_paused()
            time.sleep(1)

        # If a new location has been passed to us, get the most recent one.
        if not new_location_queue.empty():
            log.info('New location caught, moving search grid')
            try:
                while True:
                    current_location = new_location_queue.get_nowait()
            except Empty:
                pass

            step_distance = 0.9 if args.no_pokemon else 0.07

            locations = generate_hive_locations(current_location, step_distance, args.step_limit, len(scheduler_array))

            for i in range(0, len(scheduler_array)):
                scheduler_array[i].location_changed(locations[i], db_updates_queue)

        # If there are no search_items_queue either the loop has finished (or been
        # cleared above) -- either way, time to fill it back up
        for i in range(0, len(scheduler_array)):
            if scheduler_array[i].time_to_refresh_queue():
                threadStatus['Overseer']['message'] = 'Search queue {} empty, scheduling more items to scan'.format(i)
                log.debug('Search queue %d empty, scheduling more items to scan', i)
                try:  # Can't have the scheduler die because of a DB deadlock
                    scheduler_array[i].schedule()
                except Exception as e:
                    log.error('Schedule creation had an Exception: {}'.format(e))
                    traceback.print_exc(file=sys.stdout)
                    time.sleep(10)
            else:
                threadStatus['Overseer']['message'] = scheduler_array[i].get_overseer_message()

        # Now we just give a little pause here.
        time.sleep(1)


# Generates the list of locations to scan
def generate_hive_locations(current_location, step_distance, step_limit, hive_count):
    NORTH = 0
    EAST = 90
    SOUTH = 180
    WEST = 270

    xdist = math.sqrt(3) * step_distance  # dist between column centers
    ydist = 3 * (step_distance / 2)  # dist between row centers

    results = []

    results.append((current_location[0], current_location[1], 0))

    loc = current_location
    ring = 1

    while len(results) < hive_count:

        loc = get_new_coords(loc, ydist * (step_limit - 1), NORTH)
        loc = get_new_coords(loc, xdist * (1.5 * step_limit - 0.5), EAST)
        results.append((loc[0], loc[1], 0))

        for i in range(ring):
            loc = get_new_coords(loc, ydist * step_limit, NORTH)
            loc = get_new_coords(loc, xdist * (1.5 * step_limit - 1), WEST)
            results.append((loc[0], loc[1], 0))

        for i in range(ring):
            loc = get_new_coords(loc, ydist * (step_limit - 1), SOUTH)
            loc = get_new_coords(loc, xdist * (1.5 * step_limit - 0.5), WEST)
            results.append((loc[0], loc[1], 0))

        for i in range(ring):
            loc = get_new_coords(loc, ydist * (2 * step_limit - 1), SOUTH)
            loc = get_new_coords(loc, xdist * 0.5, WEST)
            results.append((loc[0], loc[1], 0))

        for i in range(ring):
            loc = get_new_coords(loc, ydist * (step_limit), SOUTH)
            loc = get_new_coords(loc, xdist * (1.5 * step_limit - 1), EAST)
            results.append((loc[0], loc[1], 0))

        for i in range(ring):
            loc = get_new_coords(loc, ydist * (step_limit - 1), NORTH)
            loc = get_new_coords(loc, xdist * (1.5 * step_limit - 0.5), EAST)
            results.append((loc[0], loc[1], 0))

        # Back to start
        for i in range(ring - 1):
            loc = get_new_coords(loc, ydist * (2 * step_limit - 1), NORTH)
            loc = get_new_coords(loc, xdist * 0.5, EAST)
            results.append((loc[0], loc[1], 0))

        loc = get_new_coords(loc, ydist * (2 * step_limit - 1), NORTH)
        loc = get_new_coords(loc, xdist * 0.5, EAST)

        ring += 1

    return results


def perform_map_request(args, status, api, step_location):
    retries = 0
    max_retries = 3
    scan_date = datetime.utcnow()
    response_dict = False

    while retries < max_retries:
        # actually increase the retry counter
        retries += 1
        is_last_retry = retries == max_retries

        scan_date = datetime.utcnow()
        response_dict = map_request(api, step_location, args.jitter)

        if not response_dict:
            if is_last_retry:
                status['message'] = 'Invalid response. Retried {} times. Giving up.'.format(max_retries)
            else:
                status['message'] = 'Invalid response. Retrying {} more times.'.format(max_retries - retries)
            log.error(status['message'])
        else:
            # if captcha we don't need to retry
            if len(response_dict['responses']['CHECK_CHALLENGE']['challenge_url']) > 1:
                return response_dict, scan_date

            # check for empty response
            # no pokemon -> possible speed violation -> retry!
            cells = response_dict['responses']['GET_MAP_OBJECTS']['map_cells']
            for cell in cells:
                # immediately return if any pokemon was found
                if len(cell.get('wild_pokemons', [])) or len(cell.get('nearby_pokemons', [])):
                    return response_dict, scan_date

            if is_last_retry:
                status['message'] = 'Got empty response. Retried {} times. Giving up.'.format(max_retries)
            else:
                status['message'] = 'Got empty response. Retrying {} more times.'.format(max_retries - retries)
            log.warning(status['message'])

        # wait constant time (ignoring scan-delay)
        if not is_last_retry:
            time.sleep(12)

    return response_dict, scan_date


def search_worker_thread(args, account_queue, account_failures, search_items_queue, pause_bit, status, dbq, whq, scheduler):

    log.debug('Search worker thread starting')

    # The outer forever loop restarts only when the inner one is intentionally exited - which should only be done when the worker is failing too often, and probably banned.
    # This reinitializes the API and grabs a new account from the queue.
    while True:
        try:
            status['starttime'] = now()

            # Get an account.
            status['message'] = 'Waiting to get new account from the queue'
            log.info(status['message'])
            # Make sure the scheduler is done for valid locations
            while not scheduler.ready:
                time.sleep(1)

            account = account_queue.get()
            status.update(WorkerStatus.get_worker(account['username'], scheduler.scan_location))
            status['message'] = 'Switching to account {}'.format(account['username'])
            log.info(status['message'])

            # Delay each thread start time so that logins occur after delay.
            loginDelayLock.acquire()
            delay = args.login_delay + ((random.random() - .5) / 2)
            log.debug('Delaying thread startup for %.2f seconds', delay)
            time.sleep(delay)
            loginDelayLock.release()

            # New lease of life right here.
            status['fail'] = 0
            status['success'] = 0
            status['noitems'] = 0
            status['skip'] = 0
            status['captchas'] = 0

            # sleep when consecutive_fails reaches max_failures, overall fails for stat purposes
            consecutive_fails = 0

            # sleep when consecutive_noitems reaches max_empty, overall noitems for stat purposes
            consecutive_noitems = 0

            # Create the API instance this will use.
            if args.mock != '':
                api = FakePogoApi(args.mock)
            else:
                api = PGoApi()

            # New account - new proxy
            if args.proxy:
                # If proxy is not assigned yet or if proxy-rotation is defined - query for new proxy
                if (not status['proxy_url']) or \
                   ((args.proxy_rotation is not None) and (args.proxy_rotation != 'none')):

                    proxy_num, status['proxy_url'] = get_new_proxy(args)
                    if args.proxy_display.upper() != 'FULL':
                        status['proxy_display'] = proxy_num
                    else:
                        status['proxy_display'] = status['proxy_url']

            if status['proxy_url']:
                log.debug("Using proxy %s", status['proxy_url'])
                api.set_proxy({'http': status['proxy_url'], 'https': status['proxy_url']})

            # The forever loop for the searches.
            while True:

                while pause_bit.is_set():
                    status['message'] = 'Scanning paused'
                    time.sleep(2)

                # If this account has been messing up too hard, let it rest
                if (args.max_failures > 0) and (consecutive_fails >= args.max_failures):
                    status['message'] = 'Account {} failed more than {} scans; possibly bad account. Switching accounts...'.format(account['username'], args.max_failures)
                    log.warning(status['message'])
                    account_failures.append({'account': account, 'last_fail_time': now(), 'reason': 'failures'})
                    break  # exit this loop to get a new account and have the API recreated

                # If this account had not find anything for too long, let it rest
                if (args.max_empty > 0) and (consecutive_noitems >= args.max_empty):
                    status['message'] = 'Account {} returned empty scan for more than {} scans; possibly ip is banned. Switching accounts...'.format(account['username'], args.max_empty)
                    log.warning(status['message'])
                    account_failures.append({'account': account, 'last_fail_time': now(), 'reason': 'empty scans'})
                    break  # exit this loop to get a new account and have the API recreated

                # If used proxy disappears from "live list" after background checking - switch account but DO not freeze it (it's not an account failure)
                if (args.proxy) and (not status['proxy_url'] in args.proxy):
                    status['message'] = 'Account {} proxy {} is not in a live list any more. Switching accounts...'.format(account['username'], status['proxy_url'])
                    log.warning(status['message'])
                    account_queue.put(account)  # experimantal, nobody did this before :)
                    break  # exit this loop to get a new account and have the API recreated

                # If this account has been running too long, let it rest
                if (args.account_search_interval is not None):
                    if (status['starttime'] <= (now() - args.account_search_interval)):
                        status['message'] = 'Account {} is being rotated out to rest.'.format(account['username'])
                        log.info(status['message'])
                        account_failures.append({'account': account, 'last_fail_time': now(), 'reason': 'rest interval'})
                        break

                # Grab the next thing to search (when available)
                step, step_location, appears, leaves, messages = scheduler.next_item(status)
                status['message'] = messages['wait']

                # Using step as a flag for no valid next location returned
                if step == -1:
                    time.sleep(scheduler.delay(status['last_scan_date']))
                    continue

                # Too soon?
                if appears and now() < appears + 10:  # Adding a 10 second grace period.
                    first_loop = True
                    paused = False
                    while now() < appears + 10:
                        if pause_bit.is_set():
                            paused = True
                            break  # why can't python just have `break 2`...
                        status['message'] = messages['early']
                        if first_loop:
                            log.info(status['message'])
                            first_loop = False
                        time.sleep(1)
                    if paused:
                        scheduler.task_done(status)
                        continue

                # Too late?
                if leaves and now() > (leaves - args.min_seconds_left):
                    scheduler.task_done(status)
                    status['skip'] += 1
                    # it is slightly silly to put this in status['message'] since it'll be overwritten very shortly after. Oh well.
                    status['message'] = messages['late']
                    log.info(status['message'])
                    # No sleep here; we've not done anything worth sleeping for. Plus we clearly need to catch up!
                    continue

                status['message'] = messages['search']
                log.debug(status['message'])

                # Let the api know where we intend to be for this loop
                # doing this before check_login so it does not also have to be done there
                # when the auth token is refreshed
                api.set_position(*step_location)

                # Ok, let's get started -- check our login status
                status['message'] = 'Logging in...'
                check_login(args, account, api, step_location, status['proxy_url'])

                # putting this message after the check_login so the messages aren't out of order
                status['message'] = messages['search']
                log.info(status['message'])

                # Make the actual request. (finally!)
                response_dict, scan_date = perform_map_request(args, status, api, step_location)
                status['last_scan_date'] = scan_date

                # Record the time and place the worker made the request at
                status['latitude'] = step_location[0]
                status['longitude'] = step_location[1]
                dbq.put((WorkerStatus, {0: WorkerStatus.db_format(status)}))

                # G'damnit, nothing back. Mark it up, sleep, carry on
                if not response_dict:
                    status['fail'] += 1
                    consecutive_fails += 1
                    status['message'] = messages['invalid']
                    log.error(status['message'])
                    time.sleep(scheduler.delay(status['last_scan_date']))
                    continue

                # Got the response, check for captcha, parse it out, then send todo's to db/wh queues.
                try:
                    # Captcha check
                    if args.captcha_solving:
                        captcha_url = response_dict['responses']['CHECK_CHALLENGE']['challenge_url']
                        if len(captcha_url) > 1:
                            status['captchas'] += 1
                            if args.captcha_solving_strategy == 'manual_only':
                                captcha_error = captcha_handling_manual(args, api, status, account, whq, captcha_url,
                                                                        step_location)
                            elif args.captcha_solving_strategy == 'automatic':
                                captcha_error = captcha_handling_2captcha(args, api, status, account, captcha_url,
                                                                          step_location)
                            else:
                                # default: manual-first
                                # first try manual solving
                                captcha_error = captcha_handling_manual(args, api, status, account, whq, captcha_url,
                                                                        step_location)
                                # then try 2Captcha
                                if captcha_error and args.captcha_key:
                                    captcha_error = captcha_handling_2captcha(args, api, status, account, captcha_url,
                                                                              step_location)

                            if captcha_error:
                                account_failures.append(
                                    {'account': account, 'last_fail_time': now(), 'reason': captcha_error})
                            else:
                                # Uncaptcha'd, but make another request for the same coordinate since the previous one was captcha'd
                                response_dict, scan_date = perform_map_request(args, status, api, step_location)
                                status['last_scan_date'] = scan_date

                    parsed = parse_map(args, response_dict, step_location, dbq, whq, api, scan_date)
                    scheduler.task_done(status, parsed)
                    if parsed['count'] > 0:
                        status['success'] += 1
                        consecutive_noitems = 0
                    else:
                        status['noitems'] += 1
                        consecutive_noitems += 1
                    consecutive_fails = 0
                    status['message'] = 'Search at {:6f},{:6f} completed with {} finds'.format(step_location[0], step_location[1], parsed['count'])
                    log.debug(status['message'])
                # except KeyError as e:
                except Exception as e:
                    parsed = False
                    status['fail'] += 1
                    consecutive_fails += 1
                    # consecutive_noitems = 0 - I propose to leave noitems counter in case of error
                    status['message'] = 'Map parse failed at {:6f},{:6f} with "{}", abandoning location. {} may be banned.'.format(step_location[0], step_location[1], repr(e), account['username'])
                    log.exception('{}. Exception message: {}'.format(status['message'], e))

                # Get detailed information about gyms.
                if args.gym_info and parsed:
                    # Build up a list of gyms to update.
                    gyms_to_update = {}
                    for gym in parsed['gyms'].values():
                        # Can only get gym details within 1km of our position.
                        distance = calc_distance(step_location, [gym['latitude'], gym['longitude']])
                        if distance < 1:
                            # Check if we already have details on this gym. (if not, get them)
                            try:
                                record = GymDetails.get(gym_id=gym['gym_id'])
                            except GymDetails.DoesNotExist as e:
                                gyms_to_update[gym['gym_id']] = gym
                                continue

                            # If we have a record of this gym already, check if the gym has been updated since our last update.
                            if record.last_scanned < gym['last_modified']:
                                gyms_to_update[gym['gym_id']] = gym
                                continue
                            else:
                                log.debug('Skipping update of gym @ %f/%f, up to date', gym['latitude'], gym['longitude'])
                                continue
                        else:
                            log.debug('Skipping update of gym @ %f/%f, too far away from our location at %f/%f (%fkm)', gym['latitude'], gym['longitude'], step_location[0], step_location[1], distance)

                    if len(gyms_to_update):
                        gym_responses = {}
                        current_gym = 1
                        status['message'] = 'Updating {} gyms for location {},{}...'.format(len(gyms_to_update), step_location[0], step_location[1])
                        log.debug(status['message'])

                        for gym in gyms_to_update.values():
                            status['message'] = 'Getting details for gym {} of {} for location {:6f},{:6f}...'.format(current_gym, len(gyms_to_update), step_location[0], step_location[1])
                            time.sleep(random.random() + 2)
                            response = gym_request(api, step_location, gym)

                            # make sure the gym was in range. (sometimes the API gets cranky about gyms that are ALMOST 1km away)
                            if response['responses']['GET_GYM_DETAILS']['result'] == 2:
                                log.warning('Gym @ %f/%f is out of range (%dkm), skipping', gym['latitude'], gym['longitude'], distance)
                            else:
                                gym_responses[gym['gym_id']] = response['responses']['GET_GYM_DETAILS']

                            # Increment which gym we're on. (for status messages)
                            current_gym += 1

                        status['message'] = 'Processing details of {} gyms for location {:6f},{:6f}...'.format(len(gyms_to_update), step_location[0], step_location[1])
                        log.debug(status['message'])

                        if gym_responses:
                            parse_gyms(args, gym_responses, whq, dbq)

                # Delay the desired amount after "scan" completion
                delay = scheduler.delay(status['last_scan_date'])
                status['message'] += ', sleeping {}s until {}'.format(delay, time.strftime('%H:%M:%S', time.localtime(time.time() + args.scan_delay)))

                time.sleep(delay)

        # Catch any process exceptions, log them, and continue the thread.
        except Exception as e:
            log.error('Exception in search_worker under account {} Exception message: {}'.format(account['username'], e))
            status['message'] = 'Exception in search_worker using account {}. Restarting with fresh account. See logs for details.'.format(account['username'])
            traceback.print_exc(file=sys.stdout)
            account_failures.append({'account': account, 'last_fail_time': now(), 'reason': 'exception'})
            time.sleep(args.scan_delay)


def captcha_handling_manual(args, api, status, account, whq, captcha_url, step_location):
    status['message'] = 'Account {} is encountering a captcha, starting manual captcha solving'.format(
        account['username'])
    if args.webhooks:
        whq.put(('captcha', {'account': account['username'], 'status': 'encounter', 'token_needed': token_needed}))
    log.warning(status['message'])

    captcha_token = token_request_manual(args)

    if 'TIMEOUT' in captcha_token:
        log.warning(
            "Unable to resolve captcha, timeout waiting for manual captcha token.")
        if args.webhooks:
            whq.put(('captcha', {'account': account['username'], 'status': 'timeout',
                                 'token_needed': token_needed}))
        return 'timeout waiting for manual captcha token'
    else:
        return verify_challenge(args, api, status, account, whq, step_location,
                                captcha_token)


def captcha_handling_2captcha(args, api, status, account, captcha_url, step_location):
    status['message'] = 'Account {} is encountering a captcha, starting 2Captcha sequence'.format(
        account['username'])
    log.warning(status['message'])

    captcha_token = token_request_2captcha(args, status, captcha_url)

    if 'ERROR' in captcha_token:
        log.warning("Unable to resolve captcha, please check your 2captcha API key and/or wallet balance")
        return 'invalid captcha token'
    else:
        return verify_challenge(args, api, status, account, False, step_location,
                                captcha_token)


def verify_challenge(args, api, status, account, whq, step_location, captcha_token):
    status['message'] = 'Retrieved captcha token, attempting to verify challenge for {}'.format(account['username'])
    log.info(status['message'])
    response = api.verify_challenge(token=captcha_token)
    if 'success' in response['responses']['VERIFY_CHALLENGE']:
        status['message'] = "Account {} successfully uncaptcha'd".format(account['username'])
        log.info(status['message'])
        scan_date = datetime.utcnow()
        if whq and args.webhooks:
            whq.put(('captcha', {'account': account['username'], 'status': 'solved', 'token_needed': token_needed}))
        return False
    else:
        status['message'] = "Account {} failed verifyChallenge, putting away account for now".format(
            account['username'])
        log.info(status['message'])
        if whq and args.webhooks:
            whq.put(('captcha', {'account': account['username'], 'status': 'failed', 'token_needed': token_needed}))
        return 'catpcha failed to verify'


def check_login(args, account, api, position, proxy_url):

    # Logged in? Enough time left? Cool!
    if api._auth_provider and api._auth_provider._ticket_expire:
        remaining_time = api._auth_provider._ticket_expire / 1000 - time.time()
        if remaining_time > 60:
            log.debug('Credentials remain valid for another %f seconds', remaining_time)
            return

    # Try to login. (a few times, but don't get stuck here)
    i = 0
    while i < args.login_retries:
        try:
            if proxy_url:
                api.set_authentication(provider=account['auth_service'], username=account['username'], password=account['password'], proxy_config={'http': proxy_url, 'https': proxy_url})
            else:
                api.set_authentication(provider=account['auth_service'], username=account['username'], password=account['password'])
            break
        except AuthException:
            if i >= args.login_retries:
                raise TooManyLoginAttempts('Exceeded login attempts')
            else:
                i += 1
                log.error('Failed to login to Pokemon Go with account %s. Trying again in %g seconds', account['username'], args.login_delay)
                time.sleep(args.login_delay)

    log.debug('Login for account %s successful', account['username'])
    time.sleep(20)


def map_request(api, position, jitter=False):
    # Create scan_location to send to the api based off of position, because tuples aren't mutable.
    if jitter:
        # Jitter it, just a little bit.
        scan_location = jitterLocation(position)
        log.debug('Jittered to: %f/%f/%f', scan_location[0], scan_location[1], scan_location[2])
    else:
        # Just use the original coordinates.
        scan_location = position

    try:
        cell_ids = util.get_cell_ids(scan_location[0], scan_location[1])
        timestamps = [0, ] * len(cell_ids)
        req = api.create_request()
        response = req.get_map_objects(latitude=f2i(scan_location[0]),
                                       longitude=f2i(scan_location[1]),
                                       since_timestamp_ms=timestamps,
                                       cell_id=cell_ids)
        response = req.check_challenge()
        response = req.get_hatched_eggs()
        response = req.get_inventory()
        response = req.check_awarded_badges()
        response = req.download_settings()
        response = req.get_buddy_walked()
        response = req.call()
        return response

    except Exception as e:
        log.warning('Exception while downloading map: %s', e)
        return False


def gym_request(api, position, gym):
    try:
        log.debug('Getting details for gym @ %f/%f (%fkm away)', gym['latitude'], gym['longitude'], calc_distance(position, [gym['latitude'], gym['longitude']]))
        req = api.create_request()
        x = req.get_gym_details(gym_id=gym['gym_id'],
                                player_latitude=f2i(position[0]),
                                player_longitude=f2i(position[1]),
                                gym_latitude=gym['latitude'],
                                gym_longitude=gym['longitude'])
        x = req.check_challenge()
        x = req.get_hatched_eggs()
        x = req.get_inventory()
        x = req.check_awarded_badges()
        x = req.download_settings()
        x = req.get_buddy_walked()
        x = req.call()
        # Print pretty(x).
        return x

    except Exception as e:
        log.warning('Exception while downloading gym details: %s', e)
        return False


def token_request_manual(args):
    global token_needed
    request_time = datetime.utcnow()

    token_needed += 1
    while request_time + timedelta(seconds=args.manual_captcha_solving_allowance_time) > datetime.utcnow():
        tokenLock.acquire()
        if args.no_server:
            # multiple instances, use get_token in map
            s = requests.Session()
            url = "{}/get_token?request_time={}&password={}".format(args.manual_captcha_solving_domain, request_time, args.manual_captcha_solving_password)
            token = str(s.get(url).text)
        else:
            # single instance, get Token directly
            token = Token.get_match(request_time)
            if token is not None:
                token = token.token
            else:
                token = ""
        tokenLock.release()
        if token != "":
            token_needed -= 1
            return token
        time.sleep(1)
    token_needed -= 1
    return 'TIMEOUT'


def token_request_2captcha(args, status, url):
    s = requests.Session()
    # Fetch the CAPTCHA_ID from 2captcha.
    try:
        captcha_id = s.post("http://2captcha.com/in.php?key={}&method=userrecaptcha&googlekey={}&pageurl={}".format(args.captcha_key, args.captcha_dsk, url)).text.split('|')[1]
        captcha_id = str(captcha_id)
    # IndexError implies that the retuned response was a 2captcha error.
    except IndexError:
        return 'ERROR'
    status['message'] = 'Retrieved captcha ID: {}; now retrieving token'.format(captcha_id)
    log.info(status['message'])
    # Get the response, retry every 5 seconds if its not ready.
    recaptcha_response = s.get("http://2captcha.com/res.php?key={}&action=get&id={}".format(args.captcha_key, captcha_id)).text
    while 'CAPCHA_NOT_READY' in recaptcha_response:
        log.info("Captcha token is not ready, retrying in 5 seconds")
        time.sleep(5)
        recaptcha_response = s.get("http://2captcha.com/res.php?key={}&action=get&id={}".format(args.captcha_key, captcha_id)).text
    token = str(recaptcha_response.split('|')[1])
    return token


def calc_distance(pos1, pos2):
    R = 6378.1  # KM radius of the earth

    dLat = math.radians(pos1[0] - pos2[0])
    dLon = math.radians(pos1[1] - pos2[1])

    a = math.sin(dLat / 2) * math.sin(dLat / 2) + \
        math.cos(math.radians(pos1[0])) * math.cos(math.radians(pos2[0])) * \
        math.sin(dLon / 2) * math.sin(dLon / 2)

    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    d = R * c

    return d


class TooManyLoginAttempts(Exception):
    pass
