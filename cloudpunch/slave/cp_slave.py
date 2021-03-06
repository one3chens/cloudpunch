import requests
import logging
import time
import json
import importlib
import os

from cloudpunch.slave import sysinfo


class CPSlave(object):

    def __init__(self, master_ip):
        self.master_ip = master_ip
        self.baseurl = 'http://%s' % master_ip

    def run(self):
        self.hostname = sysinfo.hostname()
        # Wait for master serer to be ready
        self.wait_for_master()

        # Register to master server
        self.register_to_master()

        # Infinite loop when more than one test is to be run
        while True:
            self.run_iteration()

    def wait_for_master(self):
        status = 0
        while status != 200:
            logging.info('Attempting to connect to master server %s' % self.master_ip)
            try:
                request = requests.get('%s/api/system/health' % self.baseurl, timeout=3)
                status = request.status_code
            except requests.exceptions.RequestException:
                status = 0
            if status != 200:
                time.sleep(1)
        logging.info('Connected successfully to master server')

    def register_to_master(self):
        register_body = {
            'hostname': self.hostname,
            'internal_ip': sysinfo.ip(),
            'external_ip': sysinfo.floating(),
            'role': sysinfo.role()
        }
        status = 0
        while status != 200:
            logging.info('Attempting to register to master server')
            try:
                request = requests.post('%s/api/register' % self.baseurl, json=register_body, timeout=3)
                status = request.status_code
            except requests.exceptions.RequestException:
                status = 0
            if status != 200:
                time.sleep(1)
        logging.info('Registered to master server')

    def run_iteration(self):
        # Wait for test status to be go
        self.wait_for_go()

        # Get test information from master
        config = self.get_config()

        # Log information
        self.log_info(config)
        # Save unofficial test files
        if 'test_files' in config:
            self.save_unofficial_tests(config)

        # Run the tests
        test_results = self.run_test(config)
        logging.info('All tests have finished')

        # Send results to master if required
        self.send_test_results(config, test_results)
        logging.info('Test process complete. Starting over')

    def wait_for_go(self):
        status_body = {
            'hostname': self.hostname
        }
        status = 'hold'
        while status != 'go':
            logging.info('Waiting for test status to be go')
            try:
                request = requests.post('%s/api/test/status' % self.baseurl, json=status_body, timeout=3)
                data = json.loads(request.text)
                status = data['status']
            except (requests.exceptions.RequestException, ValueError, KeyError):
                pass
            if status != 'go':
                time.sleep(1)
        logging.info('Test status is go, starting test')

    def get_config(self):
        test_body = {
            'hostname': self.hostname
        }
        status = 0
        while status != 200:
            logging.info('Attempting to get test information from master')
            try:
                request = requests.post('%s/api/test/run' % self.baseurl, json=test_body, timeout=3)
                status = request.status_code
            except requests.exceptions.RequestException:
                status = 0
            if status != 200:
                time.sleep(1)
        logging.info('Got test information from master')
        return json.loads(request.text)

    def log_info(self, config):
        config['role'] = sysinfo.role()
        logging.info('I am running the test(s) %s', ', '.join(config['test']))
        if 'test_files' in config:
            logging.info('I am running the unofficial test(s) %s', ', '.join(config['test_files'].keys()))
        logging.info('My role is %s', config['role'])
        if 'match_ip' in config:
            logging.info('My corresponding instance is %s', config['match_ip'])
        else:
            logging.info('I do not have a corresponding instance')

    def save_unofficial_tests(self, config):
        for unofficial_test in config['test_files']:
            with open('%s/%s.py' % (os.path.dirname(os.path.realpath(__file__)), unofficial_test), 'w') as f:
                f.write(config['test_files'][unofficial_test])

    def run_test(self, config):
        test_results = {}
        if config['test_mode'] == 'list':
            logging.info('I am running tests one at a time')
            threads = []
            # Add tests to thread list
            for test_name in config['test']:
                module = importlib.import_module(test_name)
                t = module.CloudPunchTest(config)
                threads.append(t)
            # Run each test thread
            for t in threads:
                if config['test_start_delay'] > 0:
                    logging.info('Waiting %s seconds for test_start_delay', config['test_start_delay'])
                    time.sleep(config['test_start_delay'])
                test_name = t.__module__
                test_name = test_name.split('.')[-1]
                logging.info('Starting test %s', test_name)
                t.start()
                t.join()
                if t.final_results:
                    test_results[test_name] = t.final_results

        elif config['test_mode'] == 'concurrent':
            logging.info('I am starting all the tests at once')
            threads = []
            # Add tests to thread list
            for test_name in config['test']:
                module = importlib.import_module('cloudpunch.slave.%s' % test_name)
                t = module.CloudPunchTest(config)
                threads.append(t)
            if config['test_start_delay'] > 0:
                logging.info('Waiting %s seconds for test_start_delay', config['test_start_delay'])
                time.sleep(config['test_start_delay'])
            # Run each test thread
            for t in threads:
                test_name = t.__module__
                test_name = test_name.split('.')[-1]
                logging.info('Starting test %s', test_name)
                t.start()
            # Wait for all tests to complete
            for t in threads:
                t.join()
                if t.final_results:
                    test_name = t.__module__
                    test_name = test_name.split('.')[-1]
                    test_results[test_name] = t.final_results
        else:
            logging.error('Unknown test mode %s', config['test_mode'])
        return test_results

    def send_test_results(self, config, test_results):
        send_results = False
        if config['server_client_mode']:
            if config['role'] == 'server' and config['servers_give_results']:
                send_results = True
            elif config['role'] == 'client':
                send_results = True
        else:
            send_results = True
        if send_results:
            if not test_results:
                logging.error('Expected to send results but no results to send')
                test_results = 'Expected to send results but no results to send'
            test_result_body = {
                'hostname': self.hostname,
                'results': test_results
            }
            status = 0
            while status != 200:
                logging.info('Attempting to send test results to master')
                try:
                    request = requests.post('%s/api/test/results' % self.baseurl, json=test_result_body, timeout=3)
                    status = request.status_code
                except requests.exceptions.RequestException:
                    status = 0
                if status != 200:
                    time.sleep(1)
            logging.info('Sent test results to master')
        else:
            logging.info('Not expected to send results')


class CPSlaveError(Exception):

    def __init__(self, message):
        super(CPSlaveError, self).__init__(message)
        self.message = message
