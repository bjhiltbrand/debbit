#!/usr/bin/env python3
import logging
import os
import random
import time
import traceback
import sys
import urllib.request
from datetime import datetime
from datetime import timedelta
from enum import Enum
from threading import Timer, Lock, Thread

import yaml  # PyYAML
from selenium import webdriver, common
from selenium.common.exceptions import TimeoutException, SessionNotCreatedException, ElementClickInterceptedException, \
    WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.wait import WebDriverWait


def main():
    update_check()

    if config['mode'] != 'burst' and config['mode'] != 'spread':
        logging.error('Set config.txt "mode" to burst or spread')
        return

    now = datetime.now()
    state = load_state(now.year, now.month)

    if not state:
        logging.info('No purchases yet complete for ' + now.strftime('%B %Y'))

    for merchant_name in state:
        cur_purchases = state[merchant_name]['purchase_count']
        logging.info(str(cur_purchases) + ' ' + merchant_name + ' ' + plural('purchase', cur_purchases) + ' complete for ' + now.strftime('%B %Y'))

    logging.info('')
    load_merchant('amazon_gift_card_reload', amazon_gift_card_reload),
    load_merchant('xfinity_bill_pay', xfinity_bill_pay)


def load_state(year, month):
    padded_month = '0' + str(month) if month < 10 else str(month)
    filename = absolute_path('state', 'debbit_' + str(year) + '_' + padded_month + '.txt')

    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f.read())
    except FileNotFoundError:
        return {}


def load_merchant(name, function):
    if name not in config:
        return

    if config[name]['enabled'] == True:  # need this to be explicitly set to True, not just any truthy value
        merchant = Merchant(name, function, config[name])

        if config['mode'] == 'spread':
            start_schedule(merchant)
        if config['mode'] == 'burst':
            Thread(target=burst_loop, args=(merchant,)).start()
    else:
        logging.info(name + ' disabled, set enabled: True in config.txt to enable.')


def burst_loop(merchant):
    suppress_logs = False
    burst_gap = merchant.burst_min_gap
    skip_time = datetime.fromtimestamp(0)

    while True:
        now = datetime.now()
        state = load_state(now.year, now.month)
        this_burst_count = merchant.burst_count

        if merchant.name not in state:
            cur_purchases = 0
            prev_burst_time = 0
        else:
            cur_purchases = state[merchant.name]['purchase_count']

            if len(state[merchant.name]['transactions']) < merchant.burst_count:
                prev_burst_time = 0
            else:
                prev_burst_time = state[merchant.name]['transactions'][merchant.burst_count * -1]['unix_time']

            for transaction in state[merchant.name]['transactions'][-min(len(state[merchant.name]['transactions']), merchant.burst_count):]:
                if transaction['unix_time'] > int(now.timestamp()) - min(merchant.burst_min_gap, 3600):
                    this_burst_count -= 1  # Program was stopped during burst within 60 minutes ago, count how many occurred within the last partial burst

        this_burst_count = min(this_burst_count, merchant.total_purchases - cur_purchases)

        if prev_burst_time < int(now.timestamp()) - burst_gap \
                and now.day >= merchant.min_day \
                and now.day <= (merchant.max_day if merchant.max_day else days_in_month[now.month] - 1) \
                and cur_purchases < merchant.total_purchases \
                and now > skip_time:

            logging.info('Now bursting ' + str(this_burst_count) + ' ' + merchant.name + ' ' + plural('purchase', this_burst_count))

            result = function_wrapper(merchant)  # First execution outside of loop so we don't sleep before first execution and don't sleep after last execution
            for _ in range(this_burst_count - 1):
                if result != Result.success:
                    break
                sleep_time = 30
                logging.info('Waiting ' + str(sleep_time) + ' seconds before next ' + merchant.name + ' purchase')
                time.sleep(sleep_time)
                result = function_wrapper(merchant)

            burst_gap = merchant.burst_min_gap + random.randint(0, int(merchant.burst_time_variance))

            if result == Result.skipped:
                skip_time = now + timedelta(days=1)

            suppress_logs = False
        elif not suppress_logs:
            log_next_burst_time(merchant, now, prev_burst_time, burst_gap, skip_time, cur_purchases)
            suppress_logs = True
        else:
            time.sleep(300)


def log_next_burst_time(merchant, now, prev_burst_time, burst_gap, skip_time, cur_purchases):
    prev_burst_plus_gap_dt = datetime.fromtimestamp(prev_burst_time + burst_gap)
    cur_month_min_day_dt = datetime(now.year, now.month, merchant.min_day)

    if now.month == 12:
        year = now.year + 1
        month = 1
    else:
        year = now.year
        month = now.month + 1

    next_month_min_day_dt = datetime(year, month, merchant.min_day)

    if now.day < merchant.min_day:
        next_burst_time = prev_burst_plus_gap_dt if prev_burst_plus_gap_dt > cur_month_min_day_dt else cur_month_min_day_dt
        next_burst_count = merchant.burst_count
    elif cur_purchases >= merchant.total_purchases or now.day > (merchant.max_day if merchant.max_day else days_in_month[now.month] - 1):
        next_burst_time = prev_burst_plus_gap_dt if prev_burst_plus_gap_dt > next_month_min_day_dt else next_month_min_day_dt
        next_burst_count = merchant.burst_count
    else:
        next_burst_time = prev_burst_plus_gap_dt
        next_burst_count = min(merchant.burst_count, merchant.total_purchases - cur_purchases)

    if next_burst_time < skip_time:
        next_burst_time = skip_time

    logging.info('Bursting next ' + str(next_burst_count) + ' ' + merchant.name + ' ' + plural('purchase', next_burst_count) + ' after ' + next_burst_time.strftime("%Y-%m-%d %I:%M%p"))


def start_schedule(merchant):
    now = datetime.now()
    state = load_state(now.year, now.month)

    if merchant.name not in state:  # first run of the month
        if now.day >= merchant.min_day:
            spread_recursion(merchant)
        else:
            start_offset = (datetime(now.year, now.month, merchant.min_day) - now).total_seconds()
            logging.info('Scheduling ' + merchant.name + ' at ' + formatted_date_of_offset(now, start_offset))
            Timer(start_offset, spread_recursion, [merchant]).start()
    elif state[merchant.name]['purchase_count'] < merchant.total_purchases and now.timestamp() - state[merchant.name]['transactions'][-1]['unix_time'] > merchant.spread_min_gap:
        spread_recursion(merchant)
    else:
        schedule_next(merchant)


def schedule_next(merchant):
    now = datetime.now()
    state = load_state(now.year, now.month)
    cur_purchases = state[merchant.name]['purchase_count'] if merchant.name in state else 0

    if cur_purchases < merchant.total_purchases:
        remaining_purchases = merchant.total_purchases - cur_purchases
        month_end = merchant.max_day if merchant.max_day else days_in_month[now.month] - 1
        remaining_secs_in_month = (datetime(now.year, now.month, month_end) - now).total_seconds()
        average_gap = remaining_secs_in_month / remaining_purchases

        time_variance = merchant.spread_time_variance
        while average_gap < time_variance * 2 and time_variance > 60:
            time_variance = time_variance / 2

        range_min = average_gap - time_variance if average_gap - time_variance > merchant.spread_min_gap else merchant.spread_min_gap
        range_max = average_gap + time_variance if average_gap + time_variance > merchant.spread_min_gap else merchant.spread_min_gap
    else:  # purchases complete for current month, schedule to start purchasing on the 2nd day of next month
        if now.month == 12:
            year = now.year + 1
            month = 1
        else:
            year = now.year
            month = now.month + 1

        range_min = (datetime(year, month, merchant.min_day) - now).total_seconds()

        if range_min <= 0:
            logging.error('Fatal error, could not determine date of next month when scheduling ' + merchant.name)
            return

        range_max = range_min + merchant.spread_time_variance

    start_offset = random.randint(int(range_min), int(range_max))
    logging.info('Scheduling next ' + merchant.name + ' at ' + formatted_date_of_offset(now, start_offset))
    logging.info('')
    Timer(start_offset, spread_recursion, [merchant]).start()


def spread_recursion(merchant):
    function_wrapper(merchant)
    schedule_next(merchant)


def record_transaction(merchant_name, amount):
    now = datetime.now()
    logging.info('Recording successful ' + merchant_name + ' purchase')

    if not os.path.exists(absolute_path('state')):
        os.mkdir(absolute_path('state'))

    padded_month = '0' + str(now.month) if now.month < 10 else str(now.month)
    filename = absolute_path('state', 'debbit_' + str(now.year) + '_' + padded_month + '.txt')

    state_write_lock.acquire()

    state = load_state(now.year, now.month)

    if merchant_name not in state:
        state[merchant_name] = {
            'purchase_count': 0,
            'transactions': []
        }

    cur_purchases = state[merchant_name]['purchase_count'] + 1
    state[merchant_name]['purchase_count'] = cur_purchases
    state[merchant_name]['transactions'].append({
        'amount': str(amount) + ' cents',
        'human_time': now.strftime("%Y-%m-%d %I:%M%p"),
        'unix_time': int(now.timestamp())
    })

    with open(filename, 'w', encoding='utf-8') as f:
        f.write(yaml.dump(state))

    state_write_lock.release()

    logging.info(str(cur_purchases) + ' ' + merchant_name + ' ' + plural('purchase', cur_purchases) + ' complete for ' + now.strftime('%B %Y'))


def amazon_gift_card_reload(driver, merchant, amount):
    logging.info('Spending ' + str(amount) + ' cents with ' + merchant.name + ' now')

    driver.get('https://www.amazon.com/asv/reload/order')
    WebDriverWait(driver, 30).until(expected_conditions.element_to_be_clickable((By.XPATH, "//button[contains(text(),'Sign In to Continue')]")))

    try:
        driver.find_element_by_xpath("//button[contains(text(),'Sign In to Continue')]").click()
    except ElementClickInterceptedException:  # spinner blocking button
        time.sleep(3)
        driver.find_element_by_xpath("//button[contains(text(),'Sign In to Continue')]").click()

    driver.find_element_by_id('ap_email').send_keys(merchant.usr)

    try:  # a/b tested new UI flow
        driver.find_element_by_id('continue').click()  # if not exists, exception is raised
    except common.exceptions.NoSuchElementException:  # a/b tested old UI flow
        pass

    WebDriverWait(driver, 30).until(expected_conditions.element_to_be_clickable((By.ID, 'ap_password')))
    driver.find_element_by_id('ap_password').send_keys(merchant.psw)
    driver.find_element_by_id('signInSubmit').click()

    handle_amazon_anti_automation_challenge(driver, merchant)

    try:  # OTP text message
        WebDriverWait(driver, 5).until(expected_conditions.element_to_be_clickable(
            (By.XPATH, "//*[contains(text(),'phone number ending in')]")))
        if driver.find_elements_by_id('auth-mfa-remember-device'):
            driver.find_element_by_id('auth-mfa-remember-device').click()

        sent_to_text = driver.find_element_by_xpath("//*[contains(text(),'phone number ending in')]").text
        logging.info(sent_to_text)
        logging.info('Enter OTP here:')
        otp = input()

        driver.find_element_by_id('auth-mfa-otpcode').send_keys(otp)
        driver.find_element_by_id('auth-signin-button').click()
    except TimeoutException:
        pass

    try:  # OTP email validation
        WebDriverWait(driver, 5).until(
            expected_conditions.element_to_be_clickable((By.XPATH, "//*[contains(text(),'One Time Pass')]")))
        otp_email = True
    except TimeoutException:
        otp_email = False

    try:
        driver.find_element_by_xpath("//*[contains(text(),'one-time pass')]").click()
        otp_email = True
    except common.exceptions.NoSuchElementException:
        pass

    if otp_email:
        if driver.find_elements_by_id('continue'):
            driver.find_element_by_id('continue').click()

        handle_amazon_anti_automation_challenge(driver, merchant)

        try:  # User may have manually advanced to gift card screen or stopped at OTP input. Handle OTP input if on OTP screen.
            WebDriverWait(driver, 5).until(
                expected_conditions.element_to_be_clickable((By.XPATH, "//*[contains(text(),'Enter OTP')]")))
            sent_to_text = driver.find_element_by_xpath("//*[contains(text(),'@')]").text
            logging.info(sent_to_text)
            logging.info('Enter OTP here:')
            otp = input()

            elem = driver.find_element_by_xpath("//input")
            elem.send_keys(otp)
            elem.send_keys(Keys.TAB)
            elem.send_keys(Keys.ENTER)
        except TimeoutException:
            pass

    try:
        WebDriverWait(driver, 5).until(
            expected_conditions.element_to_be_clickable((By.XPATH, "//*[contains(text(),'Not now')]")))
        driver.find_element_by_xpath("//*[contains(text(),'Not now')]").click()
    except TimeoutException:  # add mobile number page
        pass

    WebDriverWait(driver, 30).until(expected_conditions.element_to_be_clickable((By.ID, 'asv-manual-reload-amount')))
    driver.find_element_by_id('asv-manual-reload-amount').send_keys(cents_to_str(amount))

    for element in driver.find_elements_by_xpath("//span[contains(text(),'ending in " + merchant.card[-4:] + "')]"):
        try:  # Amazon has redundant non-clickable elements. This will try each one until one works.
            element.click()
            break
        except WebDriverException:
            pass

    driver.find_element_by_xpath("//button[contains(text(),'Reload $" + cents_to_str(amount) + "')]").click()

    time.sleep(10)  # give page a chance to load
    if 'thank-you' not in driver.current_url:
        WebDriverWait(driver, 30).until(expected_conditions.element_to_be_clickable(
            (By.XPATH, "//input[@placeholder='ending in " + merchant.card[-4:] + "']")))
        elem = driver.find_element_by_xpath("//input[@placeholder='ending in " + merchant.card[-4:] + "']")
        elem.send_keys(merchant.card)
        elem.send_keys(Keys.TAB)
        elem.send_keys(Keys.ENTER)
        WebDriverWait(driver, 30).until(expected_conditions.element_to_be_clickable(
            (By.XPATH, "//button[contains(text(),'Reload $" + cents_to_str(amount) + "')]")))
        time.sleep(1)
        driver.find_element_by_xpath("//button[contains(text(),'Reload $" + cents_to_str(amount) + "')]").click()
        time.sleep(10)  # give page a chance to load

    if 'thank-you' not in driver.current_url:
        return Result.unverified

    return Result.success


def handle_amazon_anti_automation_challenge(driver, merchant):
    try:
        WebDriverWait(driver, 5).until(
            expected_conditions.element_to_be_clickable((By.XPATH, "//*[contains(text(),'nter the characters')]")))

        if driver.find_elements_by_id('ap_password'):
            driver.find_element_by_id('ap_password').send_keys(merchant.psw)

        logging.info('amazon captcha detected')
        input('''
Anti-automation captcha detected. Please follow these steps, future runs shouldn't need captcha input if you set "close_browser: no" in config.txt.
1. Open the Firefox window that debbit created.
2. Input the captcha / other anti-automation challenges.
3. You should now be on the gift card reload page
4. Click on this terminal window and hit "Enter" to continue running debbit.
''')
    except TimeoutException:
        pass


def xfinity_bill_pay(driver, merchant, amount):
    logging.info('Spending ' + str(amount) + ' cents with ' + merchant.name + ' now')

    driver.get('https://customer.xfinity.com/#/billing/payment')

    WebDriverWait(driver, 90).until(expected_conditions.element_to_be_clickable((By.ID, 'user')))

    driver.find_element_by_id('user').send_keys(merchant.usr)
    driver.find_element_by_id('passwd').send_keys(merchant.psw)
    driver.find_element_by_id('sign_in').click()

    try:  # first time run captcha
        WebDriverWait(driver, 5).until(expected_conditions.element_to_be_clickable((By.ID, 'nucaptcha-answer')))
        logging.info('captcha detected')
        input('''
Detected first time run captcha. Please follow these one-time steps. Future runs won't need this.

1. Open the Firefox window that debbit created.
2. Enter your user, pass, and the moving characters manually.
3. Click the "Sign In" button.
4. Click on this terminal window and hit "Enter" to continue running debbit.
''')
    except TimeoutException:
        pass

    WebDriverWait(driver, 90).until(expected_conditions.element_to_be_clickable((By.ID, 'customAmount')))

    try:  # survey pop-up
        driver.find_element_by_id('no').click()
    except common.exceptions.NoSuchElementException:
        pass

    cur_balance = driver.find_element_by_xpath("//span[contains(text(), '$')]").text
    if int(''.join([c for c in cur_balance if c.isdigit()])) == 0:  # $77.84 -> 7784
        logging.error('xfinity balance is zero, will try again later.')
        return Result.skipped
    elif int(''.join([c for c in cur_balance if c.isdigit()])) < amount:
        amount = int(cur_balance[1:-3] + cur_balance[-2:])

    driver.find_element_by_id('customAmount').send_keys(cents_to_str(amount))
    driver.find_element_by_xpath("//span[contains(text(),'nding in " + merchant.card[-4:] + "')]").click()
    driver.find_element_by_xpath("//span[contains(text(),'nding in " + merchant.card[-4:] + "')]").click()
    driver.find_element_by_xpath("//button[contains(text(),'Continue')]").click()
    driver.find_element_by_xpath("//button[contains(text(),'Submit Payment')]").click()

    try:
        WebDriverWait(driver, 90).until(expected_conditions.presence_of_element_located((By.XPATH, "//*[contains(text(),'Your payment was successful')]")))
    except TimeoutException:
        return Result.unverified

    return Result.success


def formatted_date_of_offset(now, start_offset):
    return (now + timedelta(seconds=start_offset)).strftime("%Y-%m-%d %I:%M%p")


def cents_to_str(cents):
    if cents < 10:
        return '0.0' + str(cents)
    elif cents < 100:
        return '0.' + str(cents)
    else:
        return str(cents)[:-2] + '.' + str(cents)[-2:]


def function_wrapper(merchant):
    failures = 0
    threshold = 5
    while failures < threshold:
        driver = get_webdriver()
        amount = random.randint(merchant.amount_min, merchant.amount_max)
        try:
            result = merchant.function(driver, merchant, amount)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            logging.error(merchant.name + ' error: ' + traceback.format_exc())
            failures += 1

            record_failure(driver, merchant.name, traceback.format_exc(), merchant)
            close_webdriver(driver)

            if failures < threshold:
                logging.info(str(failures) + ' of ' + str(threshold) + ' ' + merchant.name + ' attempts done, trying again in ' + str(60 * failures ** 4) + ' seconds')
                time.sleep(60 * failures ** 4)  # try again in 1min, 16min, 1.3hr, 4.3hr, 10.4hr
                continue
            else:
                error_msg = merchant.name + ' failed ' + str(failures) + ' times in a row. NOT SCHEDULING MORE ' + merchant.name + '. Stop and re-run this program to try again.'
                logging.error(error_msg)
                raise Exception(error_msg) from e

        close_webdriver(driver)

        if result == Result.success:
            record_transaction(merchant.name, amount)
        elif result == Result.unverified:
            logging.error('Unable to verify ' + merchant.name + ' purchase was successful. Just in case, NOT SCHEDULING MORE ' + merchant.name + '. Stop and re-run this program to try again.')
            sys.exit(1)  # exits this merchant's thread, not entire program

        return result


def record_failure(driver, function_name, error_msg, merchant):
    if not os.path.exists(absolute_path('failures')):
        os.mkdir(absolute_path('failures'))

    filename = absolute_path('failures', datetime.now().strftime('%Y-%m-%d_%H-%M-%S-%f') + '_' + function_name)

    with open(filename + '.txt', 'w', encoding='utf-8') as f:
        f.write(version + ' ' + error_msg)

    try:
        driver.save_screenshot(filename + '.png')

        dom = driver.execute_script('return document.documentElement.outerHTML')
        dom = scrub_sensitive_data(dom, merchant)

        with open(filename + '.html', 'w', encoding='utf-8') as f:
            f.write(dom)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:
        logging.error('record_failure error: ' + traceback.format_exc())


def scrub_sensitive_data(data, merchant):
    if not data:
        return data

    return data\
        .replace(merchant.usr, '***usr***')\
        .replace(merchant.psw, '***psw***')\
        .replace(merchant.card, '***card***')\
        .replace(merchant.card[-4:], '***card***')  # last 4 digits of card


def get_webdriver():
    web_driver_lock.acquire()
    options = Options()
    options.headless = config['hide_web_browser']
    try:
        return webdriver.Firefox(options=options, executable_path=absolute_path('geckodriver'), service_log_path=os.devnull)
    except SessionNotCreatedException:
        logging.error('')
        logging.error('Firefox not found. Please install the latest version of Firefox and try again.')
        web_driver_lock.release()
        sys.exit(1)


def close_webdriver(driver):
    try:
        driver.close()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        pass

    try:
        web_driver_lock.release()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        pass


def absolute_path(*rel_paths):  # works cross platform when running source script or Pyinstaller binary
    script_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath('__file__')
    return os.path.join(os.path.dirname(script_path), *rel_paths)


def plural(word, count):
    if count == 1:
        return word
    return word + 's'


def update_check():
    try:
        latest_version = int(urllib.request.urlopen('https://jakehilborn.github.io/debbit/updates/latest.txt').read())
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        logging.error('Unable to check for updates. Check https://github.com/jakehilborn/debbit/releases if interested.')
        return

    if VERSION_INT >= latest_version:
        return

    changelog = '\n\nDebbit update available! Download latest release here: https://github.com/jakehilborn/debbit/releases\n'

    try:
        for i in range(VERSION_INT, latest_version):
            changelog += '\n' + urllib.request.urlopen('https://jakehilborn.github.io/debbit/updates/changelogs/' + str(i + 1) + '.txt').read().decode('utf-8')
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        pass

    logging.info(changelog)

    return


class Result(Enum):
    success = 'success',
    skipped = 'skipped',
    unverified = 'unverified'


class Merchant:
    def __init__(self, name, function, config_entry):
        self.name = name
        self.function = function

        self.total_purchases = config_entry['total_purchases']
        self.amount_min = config_entry['amount_min']
        self.amount_max = config_entry['amount_max']
        self.burst_count = config_entry['burst']['count']
        self.burst_min_gap = config_entry['burst']['min_gap']
        self.burst_time_variance = config_entry['burst']['time_variance']
        self.spread_min_gap = config_entry['spread']['min_gap']
        self.spread_time_variance = config_entry['spread']['time_variance']
        self.min_day = config_entry['min_day']
        self.max_day = config_entry['max_day']
        self.usr = str(config_entry['usr'])
        self.psw = str(config_entry['psw'])
        self.card = str(config_entry['card'])


version = 'v1.0.1'
VERSION_INT = 2

if __name__ == '__main__':
    # configure loggers
    logging.getLogger().setLevel(logging.INFO)
    log_format = '%(levelname)s: %(asctime)s %(message)s'

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(stdout_handler)

    file_handler = logging.FileHandler(absolute_path('debbit_log.log'))
    file_handler.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(file_handler)

    logging.info('       __     __    __    _ __ ')
    logging.info('  ____/ /__  / /_  / /_  (_) /_')
    logging.info(' / __  / _ \/ __ \/ __ \/ / __/')
    logging.info('/ /_/ /  __/ /_/ / /_/ / / /_  ')
    logging.info('\__,_/\___/_.___/_.___/_/\__/  ' + version)
    logging.info('')

    # configure global constants
    try:
        with open(absolute_path('config.txt'), 'r', encoding='utf-8') as config_f:
            config = yaml.safe_load(config_f.read())
    except FileNotFoundError:
        logging.error('config.txt not found. Please copy and rename sample_config.txt to config.txt. Then, put your credentials and debit card info in config.txt.')
        sys.exit(1)

    state_write_lock = Lock()
    web_driver_lock = Lock()
    days_in_month = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
    main()

'''
TODO

Check for internet connection post wake-up before bursting
'''
