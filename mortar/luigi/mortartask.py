# Copyright (c) 2013 Mortar Data
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.

import abc
import time

import luigi

from mortar.api.v2 import API
from mortar.api.v2 import clusters
from mortar.api.v2 import jobs

import logging
from mortar.luigi import target_factory

logger = logging.getLogger('luigi-interface')

NUM_MAP_SLOTS_PER_MACHINE = 8
NUM_REDUCE_SLOTS_PER_MACHINE = 3

NO_GIT_REF_FLAG = "not-set-flag"

class MortarTask(luigi.Task):

     def _get_api(self):
        config = luigi.configuration.get_config() 
        email = config.get('mortar', 'email')
        api_key = config.get('mortar', 'api_key')
        if config.has_option('mortar', 'host'):
            host = config.get('mortar', 'host')
            return API(email, api_key, host=host)
        else:
            return API(email, api_key)

class MortarProjectTask(MortarTask):
    """
    Task to run a Mortar job on a cluster. If the job fails, the task will exit with an error.
    """

    # default to a cluster of size 2
    cluster_size = luigi.IntParameter(default=2)

    # whether to run this job on it's own cluster
    # or to use a multi-job cluster
    # if a large enough cluster is running, it will be used,
    # otherwise, a new multi-use cluster will be started
    run_on_single_use_cluster = luigi.BooleanParameter(False)

    # whether to use spot instances when starting a cluster
    # for this job
    use_spot_instances = luigi.BooleanParameter(True)

    # run on master by default
    git_ref = luigi.Parameter(default=NO_GIT_REF_FLAG)

    # Whether to notify on completion of a job
    notify_on_job_finish = luigi.BooleanParameter(default=False)

    # interval (in seconds) to poll for job status
    job_polling_interval = luigi.IntParameter(default=5)

    # number of retries before giving up on polling
    num_polling_retries = luigi.IntParameter(default=3)

    # version of Pig to use
    pig_version = luigi.Parameter(default='0.12')

    @abc.abstractmethod
    def project(self):
        """
        Name of the mortar project to run.
        """
        raise RuntimeError("Must implement project!")

    @abc.abstractmethod
    def script(self):
        """
        Name of the script to run.
        """
        raise RuntimeError("Must implement script!")

    @abc.abstractmethod
    def is_control_script(self):
        """
        Whether this job is a control script.
        """
        raise RuntimeError("Must implement is_control_script!")

    def parameters(self):
        """
        Parameters for this Mortar job.
        """
        return {}

    def output(self):
        return [self.success_token()]

    def token_path(self):
        # override with S3 path for usage across machines or on clusters
        return "file:///tmp"

    @abc.abstractmethod
    def script_output(self):
        """
        List of targets for output of running Pigscript
        """
        raise RuntimeError("Must implement script_output!")

    def running_token(self):
        """
        Token written out to indicate a running Pigscript
        """
        return target_factory.get_target('%s/%s-%s' % (self.token_path(), self.__class__.__name__, 'Running'))

    def success_token(self):
        """
        Token written out to indicate the Pigscript has finished
        """
        return target_factory.get_target('%s/%s' % (self.token_path(), self.__class__.__name__))

    def run(self):
        """
        Run the mortar job.
        """
        api = self._get_api()
        if self.running_token().exists():
            job_id = self.running_token().open().read().strip()
        else:
            job_id = self._run_job(api)
            # to guarantee idempotence, record that the job is running
            target_factory.write_file(self.running_token(), text=job_id)
        job = self._poll_job_completion(api, job_id)
        final_job_status_code = job.get('status_code')
        # record that the job has finished
        self.running_token().remove()
        if final_job_status_code != jobs.STATUS_SUCCESS:
            for out in self.script_output():
                logger.info('Mortar script failed: removing incomplete data in %s' % out)
                out.remove()
            raise Exception('Mortar job_id [%s] failed with status_code: [%s], error details: %s' % (job_id, final_job_status_code, job.get('error')))
        else:
            target_factory.write_file(self.success_token())
            logger.info('Mortar job_id [%s] completed successfully' % job_id)

    def _git_ref(self):
        """
        Figure out value to use for git ref.  Order of precendence is:

        1. git_ref parameter is set.
        2. environment variable MORTAR_LUIGI_GIT_REF is set
        3. master
        """
        if self.git_ref != NO_GIT_REF_FLAG:
            return self.git_ref
        else:
            import os
            env_git_ref = os.environ.get('MORTAR_LUIGI_GIT_REF')
            if env_git_ref:
                return env_git_ref
            else:
                return 'master'


    def _run_job(self, api):
        cluster_type = clusters.CLUSTER_TYPE_SINGLE_JOB if self.run_on_single_use_cluster \
            else clusters.CLUSTER_TYPE_PERSISTENT
        cluster_id = None
        if not self.run_on_single_use_cluster:
            # search for a suitable cluster
            idle_clusters = self._get_idle_clusters(api, min_size=self.cluster_size)
            if idle_clusters:
                # grab the idle largest cluster that's big enough to use
                largest_cluster = sorted(idle_clusters, key=lambda c: int(c['size']), reverse=True)[0]
                logger.info('Using largest running idle cluster with cluster_id [%s], size [%s]' % \
                    (largest_cluster['cluster_id'], largest_cluster['size']))
                cluster_id = largest_cluster['cluster_id']

        if cluster_id:
            job_id = jobs.post_job_existing_cluster(api, self.project(), self.script(), cluster_id,
                git_ref=self._git_ref(), parameters=self.parameters(),
                notify_on_job_finish=self.notify_on_job_finish, is_control_script=self.is_control_script(),
                pig_version=self.pig_version)
        else:
            job_id = jobs.post_job_new_cluster(api, self.project(), self.script(), self.cluster_size,
                cluster_type=cluster_type, git_ref=self._git_ref(), parameters=self.parameters(),
                notify_on_job_finish=self.notify_on_job_finish, is_control_script=self.is_control_script(),
                pig_version=self.pig_version, use_spot_instances=self.use_spot_instances)
        logger.info('Submitted new job to mortar with job_id [%s]' % job_id)
        return job_id

    def _get_idle_clusters(self, api, min_size=0):
        return [cluster for cluster in clusters.get_clusters(api)['clusters'] \
            if (cluster.get('status_code') == clusters.CLUSTER_STATUS_RUNNING) and \
               (cluster.get('cluster_type_code') != clusters.CLUSTER_TYPE_SINGLE_JOB) and \
               (len(cluster.get('running_jobs')) == 0) and \
               (int(cluster.get('size')) >= min_size)]

    def _poll_job_completion(self, api, job_id):

        current_job_status = None
        current_progress = None

        exception_count = 0
        while True:
            try:
                # fetch job
                job = jobs.get_job(api, job_id)
                new_job_status = job.get('status_code')



                # check for updated status
                if new_job_status != current_job_status:
                    current_job_status = new_job_status
                    logger.info('Mortar job_id [%s] switched to status_code [%s], description: %s' % \
                        (job_id, new_job_status, self._get_job_status_description(job)))

                # check for updated progress on running job
                if (new_job_status == jobs.STATUS_RUNNING) and (job.get('progress') != current_progress):
                    current_progress = job.get('progress')
                    logger.info('Mortar job_id [%s] progress: [%s%%]' % (job_id, current_progress))

                # final state
                if current_job_status in jobs.COMPLETE_STATUSES:
                    return job
                else:
                    # reset exception count on successful loop
                    exception_count = 0

                    # sleep and continue polling
                    time.sleep(self.job_polling_interval)
            except Exception, e:
                if exception_count < self.num_polling_retries:
                    exception_count += 1
                    logger.info('Failure to get job status for job %s: %s' % (job_id, str(e)))
                    time.sleep(self.job_polling_interval)
                else:
                    raise

    def _get_job_status_description(self, job):
        desc = job.get('status_description')
        if job.get('status_details'):
            desc += ' - %s' % job.get('status_details')
        return desc

class MortarProjectPigscriptTask(MortarProjectTask):
    """
    Task to run a Pig script on Mortar.
    """

    def is_control_script(self):
        return False

class MortarProjectControlscriptTask(MortarProjectTask):
    """
    Task to run a control script on Mortar.
    """

    def is_control_script(self):
        return True

class MortarClusterShutdownTask(MortarTask):
    """
    Shuts down all running clusters without active jobs for the specified user.
    """

    def _get_running_idle_clusters(self, api):
        return [c for c in clusters.get_clusters(api).get('clusters') if not c.get('running_jobs')
            and c.get('status_code') == clusters.CLUSTER_STATUS_RUNNING]

    def run(self):
        api = self._get_api()
        active_clusters = self._get_running_idle_clusters(api)
        for c in active_clusters:
            logger.info('Stopping idle cluster %s' % c.get('cluster_id'))
            clusters.stop_cluster(api, c.get('cluster_id'))


