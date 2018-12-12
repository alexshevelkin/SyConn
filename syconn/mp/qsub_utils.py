# -*- coding: utf-8 -*-
# SyConn - Synaptic connectivity inference toolkit
#
# Copyright (c) 2016 - now
# Max-Planck-Institute of Neurobiology, Munich, Germany
# Authors: Philipp Schubert, Sven Dorkenwald, Jörgen Kornfeld
try:
    import cPickle as pkl
except ImportError:
    import pickle as pkl
import getpass
import glob
import numpy as np
import os
import io
import re
import shutil
import string
import subprocess
import tqdm
import sys
import time
from syconn.handler.basics import temp_seed

BACKEND_IDENT = 'SLURM'
__BATCHJOB__ = BACKEND_IDENT is not None

try:
    if BACKEND_IDENT == 'QSUB':
        cmd_check = 'qstat'
    elif BACKEND_IDENT == 'SLURM':
        cmd_check = 'squeue'
    else:
        raise NotImplementedError
    with open(os.devnull, 'w') as devnull:
        subprocess.check_call(cmd_check, shell=True,
                              stdout=devnull, stderr=devnull)
except subprocess.CalledProcessError:
    # print("QSUB not found, switching to single node multiprocessing.")
    __BATCHJOB__ = False

home_dir = os.environ['HOME'] + "/"
path_to_scripts_default = os.path.dirname(__file__)
qsub_work_folder = "%s/%s/" % (home_dir, BACKEND_IDENT)
username = getpass.getuser()
python_path = sys.executable


def QSUB_script(params, name, queue=None, pe=None, n_cores=1, priority=0,
                additional_flags='', suffix="", job_name="default",
                script_folder=None, n_max_co_processes=None, sge_additional_flags=None):
    """
    QSUB handler - takes parameter list like normal multiprocessing job and
    runs them on the specified cluster

    IMPORTANT NOTE: the user has to make sure that queues exist and work; we
    suggest to generate multiple queues handling different workloads

    Parameters
    ----------
    params: List
        list of all parameter sets to be processed
    name: str
        name of job - specifies script with QSUB_%s % name
    queue: str or None
        queue name
    pe: str
        parallel environment name
    n_cores: int
        number of cores per job submission
    priority: int
        -1024 .. 1023, job priority, higher is more important
    additional_flags: str
        additional command line flags to be passed to qsub
    suffix: str
        suffix for folder names - enables the execution of multiple qsub jobs
        for the same function
    job_name: str
        unique name for job - or just 'default' which gets changed into a
        random name automatically
    script_folder: str or None
        directory in which the QSUB_* file is located
    n_max_co_processes: int or None
        limits the number of processes that are executed on the cluster at the 
        same time; None: no limit
        
    Returns
    -------
    path_to_out: str
        path to the output directory

    """
    if sge_additional_flags is not None:
        print('"sge_additional_flags" kwarg will soon be replaced with "additional_flags". '
              'Please adapt method calls accordingly.')
        if additional_flags is not '':
            raise ValueError('Multiple flags set. Please use only "additional_flags" kwarg.')
        else:
            additional_flags = sge_additional_flags
    if job_name == "default":
        with temp_seed(hash(time.time()) % (2 ** 32 - 1)):
            letters = string.ascii_lowercase
            job_name = "".join([letters[l] for l in
                                np.random.randint(0, len(letters), 10 if BACKEND_IDENT == 'QSUB' else 8)])
            print("Random job_name created: %s" % job_name)
    else:
        print("WARNING: running multiple jobs via qsub is only supported "
              "with non-default job_names")

    if len(job_name) > 10:
        print("WARNING: Your job_name is longer than 10. job_names have "
              "to be distinguishable with only using their first 10 characters.")

    if script_folder is not None:
        path_to_scripts = script_folder
    else:
        path_to_scripts = path_to_scripts_default

    job_folder = qsub_work_folder+"/%s_folder%s/" % (name, suffix)
    if os.path.exists(job_folder):
        shutil.rmtree(job_folder, ignore_errors=True)

    path_to_script = path_to_scripts + "/QSUB_%s.py" % name
    path_to_storage = "%s/storage/" % job_folder
    path_to_sh = "%s/sh/" % job_folder
    path_to_log = "%s/log/" % job_folder
    path_to_err = "%s/err/" % job_folder
    path_to_out = "%s/out/" % job_folder

    if not os.path.exists(path_to_storage):
        os.makedirs(path_to_storage)
    if not os.path.exists(path_to_sh):
        os.makedirs(path_to_sh)
    if not os.path.exists(path_to_log):
        os.makedirs(path_to_log)
    if not os.path.exists(path_to_err):
        os.makedirs(path_to_err)
    if not os.path.exists(path_to_out):
        os.makedirs(path_to_out)

    print("Number of jobs for {}-script: {}".format(name, len(params)))
    pbar = tqdm.tqdm(total=len(params))

    # memory of finished jobs to calculate increments
    n_jobs_finished = 0
    last_diff_rp = 0
    sleep_time = 10
    for i_job in range(len(params)):
        if n_max_co_processes is not None:
            while last_diff_rp == 0:
                nb_rp = number_of_running_processes(job_name)
                last_diff_rp = n_max_co_processes - nb_rp

                if last_diff_rp == 0:
                    n_jobs_done = len(glob.glob(path_to_out + "*.pkl"))
                    diff = n_jobs_done - n_jobs_finished
                    pbar.update(diff)
                    n_jobs_finished = n_jobs_done
                    time.sleep(sleep_time)
            last_diff_rp -= 1
            sleep_time = 1

        this_storage_path = path_to_storage+"job_%d.pkl" % i_job
        this_sh_path = path_to_sh+"job_%d.sh" % i_job
        this_out_path = path_to_out+"job_%d.pkl" % i_job
        job_log_path = path_to_log + "job_%d.log" % i_job
        job_err_path = path_to_err + "job_%d.log" % i_job

        with open(this_sh_path, "w") as f:
            f.write("#!/bin/bash\n")
            f.write("{0} {1} {2} {3}".format(python_path,
                                             path_to_script,
                                             this_storage_path,
                                             this_out_path))

        with open(this_storage_path, "wb") as f:
            for param in params[i_job]:
                pkl.dump(param, f)
        # try:
        #     os.chmod(this_sh_path, 0744)
        # except SyntaxError:
        # somehow the above does not work to catch the SyntaxError (python3 compatibility)
        os.chmod(this_sh_path, 0o744)
        if BACKEND_IDENT == 'QSUB':
            if pe is not None:
                sge_queue_option = "-pe %s %d" % (pe, n_cores)
            elif queue is not None:
                sge_queue_option = "-q %s" % queue
            else:
                raise Exception("No queue or parallel environment defined")
            cmd_exec = "qsub {0} -o {1} -e {2} -N {3} -p {4} {5} {6}".format(
                sge_queue_option,
                job_log_path,
                job_err_path,
                job_name,
                priority,
                additional_flags,
                this_sh_path)
            subprocess.call(cmd_exec, shell=True)
        elif BACKEND_IDENT == 'SLURM':
            if '-V ' in additional_flags:
                print('"additional_flags" contained "-V" which is a QSUB/SGE specific flag, but SLURM was set '
                      'as batch system. Converting "-V" to "--export=ALL".')
                additional_flags.replace('-V ', '--export=ALL ')
            if not '--mem=' in additional_flags:
                mem_lim = int(128*n_cores/20)
                additional_flags += ' --mem={}G'.format(mem_lim)
                print('Memory requirements were not set explicitly. Setting to '
                      '128GB*n_cores/20={} GB'.format(mem_lim))
            if pe is not None:
                queue_option = "--ntasks-per-node %d" % n_cores
            elif queue is not None:
                queue_option = "--partition=%s" % queue
            else:
                raise Exception("No queue or parallel environment defined")
            if priority is not None and priority != 0:
                print('Priorities are not supported with SLURM.')
            cmd_exec = "sbatch {0} --output={1} --error={2} --job-name={3} {4} {5}".format(
                queue_option,
                job_log_path,
                job_err_path,
                job_name,
                additional_flags,
                this_sh_path)
            subprocess.call(cmd_exec, shell=True)
        else:
            raise NotImplementedError

    print("All jobs are submitted: %s" % name)
    while True:
        nb_rp = number_of_running_processes(job_name)
        # check actually running files
        if nb_rp == 0:
            break
        n_jobs_done = len(glob.glob(path_to_out + "*.pkl"))
        diff = n_jobs_done - n_jobs_finished
        pbar.update(diff)
        n_jobs_finished = n_jobs_done
        time.sleep(sleep_time)
    pbar.close()
    print("All batch jobs have finished: %s" % name)
    out_files = glob.glob(path_to_out + "*.pkl")
    if len(out_files) < len(params):
        print("%d jobs appear to have failed" % (len(params) - len(out_files)))
        checklist = np.zeros(len(params), dtype=np.bool)

        for p in out_files:
            checklist[int(re.findall("[\d]+", p)[-1])] = True

        print("Missing:")
        print(np.where(~checklist)[0])

        raise Exception("No success")

    return path_to_out


def number_of_running_processes(job_name):
    """
    Calculates the number of running jobs using qstat

    Parameters
    ----------
    job_name: str
        job_name as shown in qstats

    Returns
    -------
    nb_jobs: int
        number of running jobs

    """
    if BACKEND_IDENT == 'QSUB':
        cmd_stat = "qstat -u %s" % username
    elif BACKEND_IDENT == 'SLURM':
        cmd_stat = "squeue -u %s" % username
    else:
        raise NotImplementedError
    process = subprocess.Popen(cmd_stat, shell=True,
                               stdout=subprocess.PIPE)
    nb_lines = 0
    for line in io.TextIOWrapper(process.stdout, encoding="utf-8"):
        if job_name[:10 if BACKEND_IDENT == 'QSUB' else 8] in line:
            nb_lines += 1
    return nb_lines


def delete_jobs_by_name(job_name):
    """
    Deletes a group of jobs that have the same name

    Parameters
    ----------
    job_name: str
        job_name as shown in qstats

    Returns
    -------

    """
    if BACKEND_IDENT == 'QSUB':
        cmd_stat = "qstat -u %s" % username
    elif BACKEND_IDENT == 'SLURM':
        cmd_stat = "squeue -u %s" % username
    else:
        raise NotImplementedError
    process = subprocess.Popen(cmd_stat, shell=True,
                               stdout=subprocess.PIPE)
    job_ids = []
    for line in iter(process.stdout.readline, ''):
        curr_line = str(line)
        if job_name[:10] in curr_line:
            job_ids.append(re.findall("[\d]+", curr_line)[0])

    if BACKEND_IDENT == 'QSUB':
        cmd_del = "qdel "
        for job_id in job_ids:
            cmd_del += job_id + ", "
        command = cmd_del[:-2]

        subprocess.Popen(command, shell=True,
                         stdout=subprocess.PIPE)
    elif BACKEND_IDENT == 'SLURM':
        cmd_del = "scancel -n {}".format(job_name)
        subprocess.Popen(cmd_del, shell=True,
                         stdout=subprocess.PIPE)
    else:
        raise NotImplementedError


def negative_to_zero(a):
    if a > 0:
        return a
    else:
        return 0
