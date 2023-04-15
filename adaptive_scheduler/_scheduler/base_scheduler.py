"""BaseScheduler for Adaptive Scheduler."""
from __future__ import annotations

import abc
import subprocess
import sys
import textwrap
import time
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

from adaptive_scheduler._scheduler.common import run_submit
from adaptive_scheduler.utils import _progress, _RequireAttrsABCMeta

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import Any, ClassVar, Literal


_MULTI_LINE_BREAK = " \\\n    "


class BaseScheduler(metaclass=_RequireAttrsABCMeta):
    """Base object for a Scheduler.

    Parameters
    ----------
    cores : int
        Number of cores per job (so per learner.)
    run_script : str
        Filename of the script that is run on the nodes. Inside this script we
        query the database and run the learner.
    python_executable : str, default: `sys.executable`
        The Python executable that should run the `run_script`. By default
        it uses the same Python as where this function is called.
    log_folder : str, default: ""
        The folder in which to put the log-files.
    mpiexec_executable : str, optional
        ``mpiexec`` executable. By default `mpiexec` will be
        used (so probably from ``conda``).
    executor_type : str, default: "mpi4py"
        The executor that is used, by default `mpi4py.futures.MPIPoolExecutor` is used.
        One can use ``"ipyparallel"``, ``"dask-mpi"``, ``"mpi4py"``, or ``"process-pool"``.
    num_threads : int, default 1
        ``MKL_NUM_THREADS``, ``OPENBLAS_NUM_THREADS``, ``OMP_NUM_THREADS``, and
        ``NUMEXPR_NUM_THREADS`` will be set to this number.
    extra_scheduler : list, optional
        Extra ``#SLURM`` (depending on scheduler type)
        arguments, e.g. ``["--exclusive=user", "--time=1"]``.
    extra_env_vars : list, optional
        Extra environment variables that are exported in the job
        script. e.g. ``["TMPDIR='/scratch'", "PYTHONPATH='my_dir:$PYTHONPATH'"]``.
    extra_script : str, optional
        Extra script that will be executed after any environment variables are set,
        but before the main scheduler is run.

    Returns
    -------
    `BaseScheduler` object.
    """

    _ext: ClassVar[str]
    _submit_cmd: ClassVar[str]
    _options_flag: ClassVar[str]
    _cancel_cmd: ClassVar[str]

    required_attributes = ["_ext", "_submit_cmd", "_options_flag", "_cancel_cmd"]

    def __init__(
        self,
        cores: int,
        *,
        run_script: str | Path = "run_learner.py",
        python_executable: str | None = None,
        log_folder: str | Path = "",
        mpiexec_executable: str | None = None,
        executor_type: Literal[
            "ipyparallel",
            "dask-mpi",
            "mpi4py",
            "process-pool",
        ] = "mpi4py",
        num_threads: int = 1,
        extra_scheduler: list[str] | None = None,
        extra_env_vars: list[str] | None = None,
        extra_script: str | None = None,
    ) -> None:
        """Initialize the scheduler."""
        self.cores = cores
        self.run_script = Path(run_script)
        self.python_executable = python_executable or sys.executable
        self.log_folder = log_folder
        self.mpiexec_executable = mpiexec_executable or "mpiexec"
        self.executor_type = executor_type
        self.num_threads = num_threads
        self._extra_scheduler = extra_scheduler
        self._extra_env_vars = extra_env_vars
        self._extra_script = extra_script if extra_script is not None else ""
        self._JOB_ID_VARIABLE = "${JOB_ID}"

    @abc.abstractmethod
    def queue(self, *, me_only: bool = True) -> dict[str, dict]:
        """Get the current running and pending jobs.

        Parameters
        ----------
        me_only : bool, default: True
            Only see your jobs.

        Returns
        -------
        queue : dict
            Mapping of ``job_id`` -> `dict` with ``name`` and ``state``, for
            example ``{job_id: {"job_name": "TEST_JOB-1", "state": "R" or "Q"}}``.

        Notes
        -----
        This function might return extra information about the job, however
        this is not used elsewhere in this package.
        """

    @property
    def ext(self) -> str:
        """The extension of the job script."""
        return self._ext

    @property
    def submit_cmd(self) -> str:
        """Command to start a job, e.g. ``qsub fname.batch`` or ``sbatch fname.sbatch``."""
        return self._submit_cmd

    @abc.abstractmethod
    def job_script(self) -> str:
        """Get a jobscript in string form.

        Returns
        -------
        job_script : str
            A job script that can be submitted to the scheduler.
        """

    def batch_fname(self, name: str) -> Path:
        """The filename of the job script."""
        return Path(f"{name}{self.ext}")

    @staticmethod
    def sanatize_job_id(job_id: str) -> str:
        """Sanatize the job_id."""
        return job_id

    def cancel(
        self,
        job_names: list[str],
        *,
        with_progress_bar: bool = True,
        max_tries: int = 5,
    ) -> None:
        """Cancel all jobs in `job_names`.

        Parameters
        ----------
        job_names : list
            List of job names.
        with_progress_bar : bool, default: True
            Display a progress bar using `tqdm`.
        max_tries : int, default: 5
            Maximum number of attempts to cancel a job.
        """

        def to_cancel(job_names: Iterable[str]) -> list[str]:
            return [
                job_id
                for job_id, info in self.queue().items()
                if info["job_name"] in job_names
            ]

        def cancel_jobs(job_ids: list[str]) -> None:
            for job_id in _progress(job_ids, with_progress_bar, "Canceling jobs"):
                cmd = f"{self._cancel_cmd} {job_id}".split()
                returncode = subprocess.run(cmd, stderr=subprocess.PIPE).returncode
                if returncode != 0:
                    warnings.warn(
                        f"Couldn't cancel '{job_id}'.",
                        UserWarning,
                        stacklevel=2,
                    )

        job_names_set = set(job_names)
        for _ in range(max_tries):
            job_ids = to_cancel(job_names_set)
            if not job_ids:
                # no more running jobs
                break
            cancel_jobs(job_ids)
            time.sleep(0.5)

    def _mpi4py(self, name: str) -> str:
        log_fname = self.log_fname(name)
        return _MULTI_LINE_BREAK.join(
            (
                f"{self.mpiexec_executable}",
                f"-n {self.cores} {self.python_executable}",
                f"-m mpi4py.futures {self.run_script}",
                f"--log-fname {log_fname}",
                f"--job-id {self._JOB_ID_VARIABLE}",
                f"--name {name}",
            ),
        )

    def _dask_mpi(self, name: str) -> str:
        log_fname = self.log_fname(name)
        return _MULTI_LINE_BREAK.join(
            (
                f"{self.mpiexec_executable}",
                f"-n {self.cores} {self.python_executable} {self.run_script}",
                f"--log-fname {log_fname}",
                f"--job-id {self._JOB_ID_VARIABLE}",
                f"--name {name}",
            ),
        )

    def _ipyparallel(self, name: str) -> str:
        log_fname = self.log_fname(name)
        job_id = self._JOB_ID_VARIABLE
        profile = "${profile}"
        return textwrap.dedent(
            f"""\
            profile=adaptive_scheduler_{job_id}

            echo "Creating profile {profile}"
            ipython profile create {profile}

            echo "Launching controller"
            ipcontroller --ip="*" --profile={profile} --log-to-file &
            sleep 10

            echo "Launching engines"
            {self.mpiexec_executable} \\
                -n {self.cores-1} \\
                ipengine \\
                --profile={profile} \\
                --mpi \\
                --cluster-id='' \\
                --log-to-file &

            echo "Starting the Python script"
            {self.python_executable} {self.run_script} \\
                --profile {profile} \\
                --n {self.cores-1} \\
                --log-fname {log_fname} \\
                --job-id {job_id} \\
                --name {name}
            """,
        )

    def _process_pool(self, name: str) -> str:
        log_fname = self.log_fname(name)
        return f"{self.python_executable} {self.run_script} --n {self.cores} --log-fname {log_fname} --job-id {self._JOB_ID_VARIABLE} --name {name}"

    def _executor_specific(self, name: str) -> str:
        if self.executor_type == "mpi4py":
            return self._mpi4py(name)
        if self.executor_type == "dask-mpi":
            return self._dask_mpi(name)
        if self.executor_type == "ipyparallel":
            if self.cores <= 1:
                msg = (
                    "`ipyparalllel` uses 1 cores of the `adaptive.Runner` and"
                    " the rest of the cores for the engines, so use more than 1 core.",
                )
                raise ValueError(msg)
            return self._ipyparallel(name)
        if self.executor_type == "process-pool":
            return self._process_pool(name)
        msg = "Use 'ipyparallel', 'dask-mpi', 'mpi4py' or 'process-pool'."
        raise NotImplementedError(msg)

    def log_fname(self, name: str) -> Path:
        """The filename of the log (with JOB_ID_VARIABLE)."""
        if self.log_folder:
            log_folder = Path(self.log_folder)
            log_folder.mkdir(exist_ok=True)
        else:
            log_folder = Path.cwd()
        return log_folder / f"{name}-{self._JOB_ID_VARIABLE}.log"

    def output_fnames(self, name: str) -> list[Path]:
        """Scheduler output file names (with JOB_ID_VARIABLE)."""
        log_fname = self.log_fname(name)
        return [log_fname.with_suffix(".out")]

    @property
    def extra_scheduler(self) -> str:
        """Scheduler options that go in the job script."""
        extra_scheduler = self._extra_scheduler or []
        return "\n".join(f"#{self._options_flag} {arg}" for arg in extra_scheduler)

    @property
    def extra_env_vars(self) -> str:
        """Environment variables that need to exist in the job script."""
        extra_env_vars = self._extra_env_vars or []
        return "\n".join(f"export {arg}" for arg in extra_env_vars)

    @property
    def extra_script(self) -> str:
        """Script that will be run before the main scheduler."""
        return str(self._extra_script) or ""

    def write_job_script(self, name: str) -> None:
        """Writes a job script."""
        with self.batch_fname(name).open("w", encoding="utf-8") as f:
            job_script = self.job_script()
            f.write(job_script)

    def start_job(self, name: str) -> None:
        """Writes a job script and submits it to the scheduler."""
        self.write_job_script(name)
        submit_cmd = f"{self.submit_cmd} {self.batch_fname(name)}"
        run_submit(submit_cmd)

    def __getstate__(self) -> dict[str, Any]:
        """Return the state of the scheduler."""
        return {
            "cores": self.cores,
            "run_script": self.run_script,
            "python_executable": self.python_executable,
            "log_folder": self.log_folder,
            "mpiexec_executable": self.mpiexec_executable,
            "executor_type": self.executor_type,
            "num_threads": self.num_threads,
            "extra_scheduler": self._extra_scheduler,
            "extra_env_vars": self._extra_env_vars,
            "extra_script": self._extra_script,
        }

    def __setstate__(self, state: dict[str, Any]) -> None:
        """Set the state of the scheduler."""
        self.__init__(**state)  # type: ignore[misc]