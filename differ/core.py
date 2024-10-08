import os
import shlex
import signal
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from functools import cached_property
from itertools import chain
from pathlib import Path
from typing import Any, Iterator, Optional, Union
from uuid import uuid4

import jinja2
import yaml

from .template import JINJA_ENVIRONMENT


class TraceHook:
    """
    A hook that is called prior to running a trace and after the trace has executed.
    """

    def setup(self, trace: 'Trace') -> None:
        """
        Run any setup actions prior to executing a trace.
        """

    def teardown(self, trace: 'Trace') -> None:
        """
        Run any teardown actions after a trace has completed.
        """


@dataclass
class DebloatedBinary:
    """
    A debloated binary to evaluate.
    """

    #: Debloater engine name
    engine: str
    #: Debloated binary
    binary: Path

    @classmethod
    def load_dict(cls, name: str, body: Union[str, dict]) -> 'DebloatedBinary':
        """
        Load the debloated binary from a dictionary.

        :param name: the debloater engine name
        :param body: debloated binary dictionary
        """
        if isinstance(body, str):
            return cls(name, Path(body))
        return cls(name, Path(body['binary']))


@dataclass
class Project:
    """
    A fuzzing project containing the original binary and multiple recovered versions.
    """

    #: Unique project name
    name: str
    # Image name
    image_name: str
    #: Project directory
    directory: Path
    #: Original binary file path
    original: Path
    #: List of debloated binaries
    debloaters: dict[str, DebloatedBinary] = field(default_factory=dict)
    #: List of the traces to run
    templates: list['TraceTemplate'] = field(default_factory=list)
    #: Target filename for each binary that is executed.
    link_filename: str = ''
    #: The binary version
    version: str = ''

    def context_directory(self, context: 'TraceContext') -> Path:
        """
        :returns: the trace context working directory
        """
        return self.directory / f'trace-{context.id}'

    def trace_directory(self, context: 'TraceContext', debloater_engine: str) -> Path:
        """
        :returns: the trace working directory within a context
        """
        return self.context_directory(context) / debloater_engine

    def crash_filename(self, trace: 'Trace') -> Path:
        """
        :returns: the crash report filename when the original binary does not behave correctly
        """
        return self.directory / f'crash-{trace.debloater_engine}-{trace.context.id}.yml'

    def report_filename(self, trace: 'Trace', successful: bool) -> Path:
        """
        :returns: the report filename for a trace
        """
        engine = trace.debloater_engine
        if successful:
            engine += '-success'
        else:
            engine += '-error'

        return self.directory / f'report-{engine}-{trace.context.id}.yml'

    def save_report(self, trace: 'Trace', results: list['ComparisonResult']) -> None:
        """
        Save the list of comparison results to a YAML report.

        :param trace: trace that was executed
        :param results: trace comparison results
        """
        docs = []
        if trace.process:
            # The process has already executed
            args = trace.process.args[1:]  # type: ignore
        else:
            # The process did not execute. This should not happen and is here as a fallback
            args = shlex.split(trace.arguments)

        body = {
            'values': trace.context.values,
            'trace_directory': str(trace.cwd),
            'results': docs,
            'arguments': args,
            'binary': str(trace.binary.readlink()),
        }
        for result in results:
            if isinstance(result.comparator, Comparator):
                name = result.comparator.id
            else:
                name = result.comparator

            doc = {
                'comparator': name,
                'details': result.details,
                'status': result.status.value,
            }
            docs.append(doc)

        successful = all(result for result in results)
        with open(self.report_filename(trace, successful), 'w') as file:
            file.write(yaml.safe_dump(body))

    @classmethod
    def load(cls, report_directory: Path, filename: Union[str, Path]) -> 'Project':
        """
        Load the project from a YAML file and resolve all relative paths within the configurations.

        :param report_directory: root directory for storing all report data
        :param filename: project filename
        :returns: the parsed project object
        """
        with open(filename, 'r') as file:
            body = yaml.safe_load(file)

        project = cls._load_dict(body)
        project.resolve_paths(Path(filename).parent, report_directory)

        return project

    def resolve_paths(self, project_directory: Path, report_directory: Path) -> None:
        """
        Resolve all relative paths within the project definition.

        :param project_directory: the project directory where the YAML file is loaded from
        :param report_directory: the report directory where reports are stored
        """
        # First, resolve input paths against the project directory
        if not self.original.is_absolute():
            self.original = (project_directory / self.original).resolve()

        for debloater in self.debloaters.values():
            if not debloater.binary.is_absolute():
                # Resolve the debloated binary to an absolute file path, relative to the directory
                # where the project file is located.
                debloater.binary = (project_directory / debloater.binary).absolute()

        for template in self.templates:
            for input_file in template.input_files:
                input_file.resolve_source(project_directory)

        if not self.directory.is_absolute():
            self.directory = (report_directory / self.directory).resolve()

    @classmethod
    def _load_dict(cls, body: dict) -> 'Project':
        """
        Load a project from a dictionary.

        :param body: project dictionary object
        :returns: the parsed project object
        """
        original = Path(body['original'])
        templates = [
            TraceTemplate.load_dict(item, template_id=f'{id:03}')
            for id, item in enumerate(body['templates'], start=1)
        ]
        debloaters = {
            name: DebloatedBinary.load_dict(name, value)
            for name, value in body['debloaters'].items()
        }
        version = str(body.get('version', ''))

        return cls(
            name=body['name'],
            image_name=body.get('image_name', ''),
            original=original,
            debloaters=debloaters,
            templates=templates,
            directory=Path(body['name']),
            link_filename=body.get('link_filename', ''),
            version=version,
        )


@dataclass
class InputFile:
    """
    An input file to a trace template. Input files are either static, where they are copied to each
    trace directory without modification, or dynamic and generated on a per-trace basis using the
    Jinja2 template engine and the trace context variable values.
    """

    #: Source path to the input file. This can be relative to the project YAML file being loaded.
    source: Path
    #: The destination of the file for each trace. This can be relative to the trace directory or
    #: an absolute path. The source file is stored in the trace directory with the same basename if
    #: a destination is not specified.
    destination: Optional[Path] = None
    #: The destination file mode. The source file's mode are copied if not specified.
    mode: Optional[str] = None
    #: The file is static and should not be generated using the trace context variables.
    static: bool = False

    def __post_init__(self) -> None:
        self._template: Optional[jinja2.Template] = None

    def resolve_source(self, cwd: Path) -> None:
        """
        Resolve the source filename to an absolute file path based on the provided working
        directory.

        :param cwd: working directory to resolve relative paths against
        """
        if not self.source.is_absolute():
            self.source = (cwd / self.source).resolve()

    @cached_property
    def template(self) -> jinja2.Template:
        """
        :returns: the Jinja2 template object for the input file
        """
        return JINJA_ENVIRONMENT.from_string(self.source.read_text())

    @classmethod
    def load_dict(cls, body: Union[dict, str]) -> 'InputFile':
        """
        Load the input file from a dictionary or string.

        :param body: the input file dictionary or the source file path
        :returns: the parsed input file
        """
        if isinstance(body, str):
            return cls(Path(body))

        destination = body.get('destination')
        mode = body.get('mode')
        if isinstance(mode, int):
            mode = str(mode)

        return cls(
            source=Path(body['source']),
            destination=Path(destination) if destination else None,
            mode=mode,
            static=body.get('static', False),
        )

    def get_destination(self, cwd: Path) -> Path:
        """
        Get the destination path, relative to the ``cwd`` if the destination is relative.

        :param cwd: working directory to resolve relative paths against
        :returns: the absolute file path to the destination
        """
        if not self.destination:
            return cwd / self.source.name

        if self.destination.is_absolute():
            dest = self.destination
        else:
            dest = (cwd / self.destination).resolve()

        if dest.exists() and dest.is_dir():
            dest = dest / self.source.name

        return dest


@dataclass
class TimeoutConstraint:
    """
    The amount of time that each trace is allowed to execute and how to treat traces that exceed
    the timeout.
    """

    #: Number of seconds before a timeout occurs
    seconds: int = 60
    #: The timeout it expected and should not be treated as an error
    expected: bool = False

    @classmethod
    def load_dict(cls, body: Union[int, dict]) -> 'TimeoutConstraint':
        """
        Load from a dictionary.
        """
        if isinstance(body, int):
            return cls(seconds=body)

        return cls(**body)


class ConcurrentHookMode(Enum):
    client = 'client'

    @classmethod
    def parse(cls, val: Optional[str]) -> Optional['ConcurrentHookMode']:
        if not val:
            return None
        return cls(val)


@dataclass
class ConcurrentHook:
    """
    The concurrent process to execute as the trace is running.
    """

    #: The bash script to execute
    run: str
    #: The delay time, in seconds, prior to launching the concurrent process
    delay: float = 1.0
    #: The connect script mode
    mode: Optional[ConcurrentHookMode] = None
    #: The number of times that the concurrent script is retried before failing. This option is
    #: useful for testing network clients where the server needs some time to start and be ready to
    #: accept incoming connections.
    retries: int = 0

    @classmethod
    def load_dict(cls, body: Union[str, dict]) -> 'ConcurrentHook':
        if isinstance(body, str):
            return cls(body)
        return cls(
            body['run'],
            float(body.get('delay', 1.0)),
            mode=ConcurrentHookMode.parse(body.get('mode')),
            retries=body.get('retries', 0),
        )


@dataclass
class PcapConfig:
    """
    Configuration for capturing packets during trace execution.
    """

    #: The filename to store the pcap to, relative to the trace working directory
    filename: Path
    #: The network interface to capture on
    interface: str

    @classmethod
    def parse(cls, body: dict) -> 'PcapConfig':
        return cls(filename=Path(body['filename']), interface=body['interface'])


@dataclass
class TraceTemplate:
    """
    A trace configuration template.
    """

    #: Command line arguments
    arguments: str = ''
    #: Fuzzing input variables
    variables: dict[str, 'FuzzVariable'] = field(default_factory=dict)
    #: List of comparators to validate the original against each recovered binary
    comparators: list['Comparator'] = field(default_factory=list)
    #: Expect a successful run on both the original and the recovered binary. If this is ``False``,
    #: then the recovered binary is expected to fail.
    expect_success: bool = True
    #: List of input files that are used within each trace.
    input_files: list[InputFile] = field(default_factory=list)
    #: Standard input content or input file path. The YAML must be a dictionary with either a
    #: ``file`` key that contains the path to the file or ``value`` that contains the string value.
    #: The ``file`` key may be absolute, relative to the trace working directory, or reference an
    #: input file that is generated.
    stdin: Union[str, Path] = ''
    #: Defines how long the process is allowed to run and if a timeout is expected
    timeout: TimeoutConstraint = field(default_factory=TimeoutConstraint)
    #: Bash script commands to execute prior to running a trace
    setup: str = ''
    #: Bash script commands to execute after a trace has finished
    teardown: str = ''
    #: The concurrent hook configuration
    concurrent: Optional[ConcurrentHook] = None
    #: Exit immediately when the first command fails within all hook scripts (setup, concurrent,
    #: and teardown). When this is enabled, all bash scripts have the ``set -e`` option set.
    script_exit_on_first_error: bool = True
    # Treat the binary exiting from the specified signal as normal.
    expect_signal: int = 0
    #: Packet capture configuration
    pcap: Optional[PcapConfig] = None
    #: User supplied name
    name: str = ''
    #: A brief summary of the template
    summary: str = ''
    #: Autogenerated id
    id: str = field(default_factory=lambda: str(uuid4()))

    def __str__(self):
        return self.id

    @cached_property
    def arguments_template(self) -> jinja2.Template:
        """
        :returns: the Jinja2 template object for the command line arguments
        """
        return JINJA_ENVIRONMENT.from_string(self.arguments)

    @cached_property
    def stdin_template(self) -> Optional[jinja2.Template]:
        """
        :returns: the Jinja2 template object for the stdin content if the ``stdin`` is a non-empty
            string, ``None`` otherwise
        """
        if self.stdin:
            content = self.stdin.read_text() if isinstance(self.stdin, Path) else self.stdin
            return JINJA_ENVIRONMENT.from_string(content)
        return None

    @cached_property
    def setup_template(self) -> Optional[jinja2.Template]:
        """
        :returns: the Jinja2 template object for the setup commands if the ``setup`` is a non-empty
            string, ``None`` otherwise
        """
        if self.setup:
            if self.script_exit_on_first_error:
                content = f'set -e\n\n{self.setup}'
            else:
                content = self.setup
            return JINJA_ENVIRONMENT.from_string(content)
        return None

    @cached_property
    def teardown_template(self) -> Optional[jinja2.Template]:
        """
        :returns: the Jinja2 template object for the teardown commands if the ``teardown`` is a
            non-empty string, ``None`` otherwise
        """
        if self.teardown:
            if self.script_exit_on_first_error:
                content = f'set -e\n\n{self.teardown}'
            else:
                content = self.teardown
            return JINJA_ENVIRONMENT.from_string(content)
        return None

    @cached_property
    def concurrent_template(self) -> Optional[jinja2.Template]:
        """
        :returns: the Jinja2 template object for the concurrent commands if the ``concurrent`` is
            specified, ``None`` otherwise
        """
        if self.concurrent:
            if self.script_exit_on_first_error:
                content = f'set -e\n\n{self.concurrent.run}'
            else:
                content = self.concurrent.run
            return JINJA_ENVIRONMENT.from_string(content)
        return None

    @property
    def hooks(self) -> Iterator[TraceHook]:
        """
        :returns: a generator that yields the hooks the need to execute prior to after a trace.
        """
        yield from chain(self.variables.values(), self.comparators)

    @classmethod
    def load_dict(cls, body: dict, template_id: str = '') -> 'TraceTemplate':
        """
        Load the trace template from a dictionary.
        """
        arguments: str = body.get('arguments', '')
        variables_config = body.get('variables', {})
        comparators_config = body.get('comparators', [])

        variables: dict[str, FuzzVariable] = {}
        for name, config in variables_config.items():
            if isinstance(config, dict):
                id = config.pop('type')
            else:
                id = str(config)
                config = {}

            variable_cls = VARIABLE_TYPE_REGISTRY[id]
            variables[name] = variable_cls(name, config)

        comparators: list[Comparator] = []
        for config in comparators_config:
            if isinstance(config, dict):
                id = config.pop('id')
            else:
                id = str(config)
                config = {}

            comparator_cls = COMPARATOR_TYPE_REGISTRY[id]
            comparators.append(comparator_cls(config))

        input_files: list[InputFile] = []
        for input_file in body.get('input_files', []):
            input_files.append(InputFile.load_dict(input_file))

        stdin_dict = body.get('stdin')
        stdin = ''
        if stdin_dict:
            if path := stdin_dict.get('file'):
                stdin = Path(path)
            elif string := stdin_dict.get('value'):
                stdin = string

        kwargs = {}
        if name := body.get('name'):
            kwargs['id'] = f'{name}-{template_id}' if template_id else name
            kwargs['name'] = name
        elif template_id:
            kwargs['id'] = template_id

        timeout_dict = body.get('timeout')
        if timeout_dict:
            timeout = TimeoutConstraint.load_dict(timeout_dict)
        else:
            timeout = TimeoutConstraint()

        concurrent_dict = body.get('concurrent')
        concurrent = ConcurrentHook.load_dict(concurrent_dict) if concurrent_dict else None

        expect_signal = body.get('expect_signal')
        if isinstance(expect_signal, str):
            expect_signal = signal.Signals[expect_signal].value
        elif isinstance(expect_signal, int):
            expect_signal = signal.Signals(expect_signal).value
        else:
            expect_signal = 0

        if pcap_config := body.get('pcap'):
            pcap = PcapConfig.parse(pcap_config)
        else:
            pcap = None

        return cls(
            arguments=arguments,
            variables=variables,
            comparators=comparators,
            input_files=input_files,
            stdin=stdin,
            timeout=timeout,
            setup=body.get('setup', ''),
            teardown=body.get('teardown', ''),
            concurrent=concurrent,
            expect_success=body.get('expect_success', True),
            script_exit_on_first_error=body.get('script_exit_on_first_error', True),
            expect_signal=expect_signal,
            pcap=pcap,
            summary=body.get('summary', '').strip(),
            **kwargs,
        )


@dataclass
class TraceContext:
    """
    Concrete parameters of a trace template. Each :class:`TraceTemplate` will create multiple trace
    context objects, where each context contains concrete values for each variable in a unique
    permutation.
    """

    #: Origin trace template
    template: TraceTemplate
    #: Concrete variable values
    values: dict[str, Any] = field(default_factory=dict)
    #: Unique context id which is auto generated if not specified
    id: str = field(default_factory=lambda: str(uuid4()))

    def __str__(self) -> str:
        return self.id

    def save(self, filename: Path) -> None:
        """
        Save the context parameters to a file.
        """
        body = {'id': self.id, 'arguments': self.template.arguments, 'values': self.values}
        with open(filename, 'w') as file:
            file.write(yaml.safe_dump(body))


@dataclass
class Trace:
    """
    A execution of a binary within a trace context. Each :class:`TraceContext` is executed once for
    each debloated binary and once for the original binary.
    """

    #: Path to the binary
    binary: Path
    #: Trace context, containing the concrete parameters.
    context: TraceContext
    #: The working directory where the sample is run
    cwd: Path
    #: The debloater engine used on the binary
    debloater_engine: str
    # Image name
    image_name: str = ''
    #: The command line arguments
    arguments: str = ''
    #: The subprocess
    process: Optional[subprocess.Popen] = None
    #: Process status, populated after the process exits. See :func:`os.waitpid`.
    process_status: int = 0
    #: The process timed out
    timed_out: bool = False
    #: Cache that is cleaned up when the trace is no longer needed. Comparators can use the cache
    #: to store the results of an expensive task.
    cache: dict[str, Any] = field(default_factory=dict)
    #: Subprocess for the setup script
    setup_script: Optional[subprocess.CompletedProcess] = None
    #: Subprocess for the teardown script
    teardown_script: Optional[subprocess.CompletedProcess] = None
    #: Subprocess for the concurrent script
    concurrent_script: Optional[subprocess.Popen] = None
    #: The timestamp when the trace began executing
    start_time: float = 0.0

    def __str__(self) -> str:
        return f'{self.context.id}[{self.debloater_engine}]'

    @property
    def crashed(self) -> bool:
        return os.WIFSIGNALED(self.process_status)

    @property
    def crash_signal(self) -> Optional[signal.Signals]:
        if self.crashed:
            return signal.Signals(os.WTERMSIG(self.process_status))
        return None

    @property
    def crash_result(self) -> Optional['CrashResult']:
        if (signal := self.crash_signal) and signal.value != self.context.template.expect_signal:
            return CrashResult(self, f'process exit from signal {signal.name} ({signal.value})')

    def read_stdout(self, cache: bool = True) -> bytes:
        """
        Read the process's recorded standard output and optionally cache the content in memory to
        avoid duplicate read operations.

        :param cache: attempt to read the content from the cache and update the cache when finished
        :returns: the process's recorded standard output
        """
        if cache:
            stdout = self.cache.get('stdout')
            if stdout is not None:
                return stdout

        stdout = self.stdout_path.read_bytes()
        if cache:
            self.cache['stdout'] = stdout
        return stdout

    def read_stderr(self, cache: bool = True) -> bytes:
        """
        Read the process's recorded standard error and optionally cache the content in memory to
        avoid duplicate read operations.

        :param cache: attempt to read the content from the cache and update the cache when finished
        :returns: the process's recorded standard error
        """
        if cache:
            stderr = self.cache.get('stderr')
            if stderr is not None:
                return stderr

        stderr = self.stderr_path.read_bytes()
        if cache:
            self.cache['stderr'] = stderr
        return stderr

    @cached_property
    def stdout_path(self) -> Path:
        """
        :returns: the path to the process's standard output
        """
        return self.cwd / '__differ-stdout.bin'

    @cached_property
    def stderr_path(self) -> Path:
        """
        :returns: the path to the process's standard error
        """
        return self.cwd / '__differ-stderr.bin'

    @cached_property
    def default_stdin_path(self) -> Path:
        """
        :returns: the default path to the trace's standard input file
        """
        return self.cwd / '__differ-stdin.bin'

    @cached_property
    def setup_script_path(self) -> Path:
        """
        :returns: the path to the trace's setup script
        """
        return self.cwd / '__differ-setup.sh'

    @cached_property
    def teardown_script_path(self) -> Path:
        """
        :returns: the path to the trace's teardown script
        """
        return self.cwd / '__differ-teardown.sh'

    @cached_property
    def pcap_path(self) -> Path:
        """
        :returns: the path to the pcap output file
        :raises TypeError: the template is not configured to capture packets
        """
        pcap = self.context.template.pcap
        if not pcap:
            raise TypeError('trace template does not have a pcap configuration')

        if pcap.filename.is_absolute():
            return pcap.filename
        return self.cwd / pcap.filename

    @cached_property
    def setup_script_output_path(self) -> Path:
        return self.cwd / '__differ-setup-output.bin'

    @cached_property
    def teardown_script_output_path(self) -> Path:
        return self.cwd / '__differ-teardown-output.bin'

    @cached_property
    def concurrent_script_path(self) -> Path:
        return self.cwd / '__differ-concurrent.sh'

    @cached_property
    def concurrent_script_output_path(self) -> Path:
        return self.cwd / '__differ-concurrent-output.bin'

    def env(self, inherit: bool = True) -> dict[str, str]:
        """
        The environment variables to use when executing all hook scripts (setup, teardown, and
        concurrent).

        :param inherit: inherit from the current process's environment variables
        :returns: the environment variables for the trace hook scripts
        """
        env = dict(os.environ) if inherit else {}

        env.update(
            {
                'DIFFER_TRACE_DIR': str(self.cwd),
                'DIFFER_TRACE_DEBLOATER': self.debloater_engine,
                'DIFFER_TRACE_BINARY': str(self.binary),
                'DIFFER_CONTEXT_ID': self.context.id,
            }
        )
        if self.process:
            env['DIFFER_TRACE_STDOUT'] = str(self.stdout_path.absolute())
            env['DIFFER_TRACE_STDERR'] = str(self.stderr_path.absolute())
            env['DIFFER_TRACE_PID'] = str(self.process.pid)
            if self.process.returncode is not None:
                env['DIFFER_TRACE_EXIT_CODE'] = str(self.process.returncode)

        if self.concurrent_script:
            env['DIFFER_CONCURRENT_PID'] = str(self.concurrent_script.pid)
            if self.concurrent_script.returncode is not None:
                env['DIFFER_CONCURRENT_EXIT_CODE'] = str(self.concurrent_script.returncode)

        return env


class ComparisonStatus(Enum):
    """
    A :class:`Comparator` status. This enumeration signals whether the results from the debloated
    binary match the original's.
    """

    #: The comparison was successful (e.g.- the debloated output matches the original's)
    success = 'success'
    #: The comparison failed (e.g.- the debloated output does not match the original's)
    error = 'error'


@dataclass
class ComparisonResult:
    """
    The result of a single comparison of a debloated binary against the original.
    """

    #: The comparison status
    status: ComparisonStatus
    #: The comparator that produced the result
    comparator: str
    #: The debloated trace
    trace: Trace
    #: Additional details
    details: str = ''

    @classmethod
    def error(
        cls, comparator: Union[str, 'Comparator'], trace: Trace, details: str = ''
    ) -> 'ComparisonResult':
        """
        Create a failed comparison result.

        :param comparator: the comparator that produced the result
        :param trace: the debloated binary trace
        :param details: additional details
        """
        return cls(
            status=ComparisonStatus.error,
            comparator=comparator.id if isinstance(comparator, Comparator) else str(comparator),
            trace=trace,
            details=details,
        )

    @classmethod
    def success(
        cls, comparator: Union[str, 'Comparator'], trace: Trace, details: str = ''
    ) -> 'ComparisonResult':
        """
        Create a successful comparison result.

        :param comparator: the comparator that produced the result
        :param trace: the debloated binary trace
        :param details: additional details
        """
        return cls(
            status=ComparisonStatus.success,
            comparator=comparator.id if isinstance(comparator, Comparator) else str(comparator),
            trace=trace,
            details=details,
        )

    def __bool__(self) -> bool:
        """
        :returns: ``True`` if the comparison was successful, ``False`` otherwise
        """
        return self.status is ComparisonStatus.success


@dataclass
class CrashResult:
    """
    A crash or unexpected result produced by the binary. This class is used when the
    original or debloated binary crashed or when the original binary did not behave as expected.
    """

    #: The trace working directory for the original binary
    trace: Trace
    #: Additional details
    details: str = ''
    comparator: Optional[Union['Comparator', str]] = None

    def save(self, filename: Path) -> None:
        """
        Save the crash result to a YAML file.

        :param filename: destination filename
        """
        body = {
            'values': self.trace.context.values,
            'trace_directory': str(self.trace.cwd),
            'details': self.details,
            'arguments': shlex.split(self.trace.arguments),
        }
        if self.comparator:
            body['comparator'] = (
                self.comparator.id
                if isinstance(self.comparator, Comparator)
                else str(self.comparator)
            )

        with open(filename, 'w') as file:
            file.write(yaml.safe_dump(body))


class FuzzVariable(TraceHook):
    """
    An input variable that has multiple values generated when evaluating a debloated binary against
    the original. Subclasses must implement two methods:

    - ``__init__()`` - parse the configuration and setup how values will be generated
    - ``generate_values()`` - yield generated values based on the configuration

    A fuzz variable is also a :class:`TraceHook` subclass so subclasses can implement the
    :meth:`~TraceHook.setup` and :meth:`~TraceHook.teardown` methods to run any actions before a
    trace is run and after it has completed.

    Fuzz variables should be bounded to yield a set number of values, such as limiting the number
    of values that are generated with a ``size`` configuration parameter.
    """

    id: str = ''

    def __init__(self, name: str, config: dict):
        """
        :param name: the variable name
        :param config: the variable configuration
        """
        self.name = name

    def generate_values(self, template: TraceTemplate) -> Iterator:
        """
        Generate concrete values for the template.

        :param template: trace template
        :returns: a generator that yields generated values
        """
        raise NotImplementedError()


class Comparator(TraceHook):
    """
    Rules and logic for comparing a debloated binary trace against the original binary trace, such
    as comparing the standard output or exit code, to verify that the debloated behavior matches
    the original.

    Subclasses must implement three methods:

    - ``__init__`` - parse the configuration and setup the comparator
    - ``verify_original`` - verify that the original trace output matches what is expected
    - ``compare`` - compare the debloated trace against the original

    Comparators can use the :data:`Trace.cache` dictionary to cache data between runs.
    """

    id: str = ''

    def __init__(self, config: dict):
        """
        :param config: comparator configuration
        """
        pass

    def verify_original(self, original: Trace) -> Optional[CrashResult]:
        """
        Verify that the original trace behaved as expected and return a crash result if the
        original deviated from the expected behavior.

        :param original: the original trace
        :returns: a crash result on error
        """
        pass

    def compare(self, original: Trace, debloated: Trace) -> ComparisonResult:
        """
        Compare a debloated binary's trace against the original and return a comparison result.

        :param original: the original trace
        :param debloated: the debloated trace
        :returns: the comparison result (success or error with details)
        """
        raise NotImplementedError()


class VariableRef:
    """
    A reference to a variable within a comparator configuration. Comparator implementations can use
    this class if some configuration can depend on a variable value that is generated for the
    active context. For example, a template can be setup to randomly create a directory name based
    on a fuzz variable. The comparator will need to check that the directory exists based on the
    generated name. In this case, the following configuration can be used:

    .. code-block:: yaml

        name: mkdir
        original: /usr/bin/mkdir

        templates:
          - arguments: "{{dirname}}"
            variables:
              dirname:
                type: str
                regex:
                  pattern: '[A-Z][a-z0-9]{3,9}'
                  count: 10

            comparators:
              - id: file
                filename:
                  variable: dirname
                exists: true
                type: directory

    In this case, the :class:`~differ.comparators.files.FileComparator` will verify that the
    directory ``{{dirname}}`` is created for each generated context.
    """

    def __init__(self, variable: str):
        """
        :param variable: the variable name
        """
        self.variable = variable

    def get(self, values: dict) -> Any:
        """
        Get the variable value from the context values dictionary. The default implementation
        returns ``values[sef.variable]`` and subclass may customize this behavior.

        :param values: context variable values
        :returns: the variable value
        """
        return values[self.variable]

    @classmethod
    def deref(cls, potential_ref: Any, values: dict) -> Any:
        """
        Attempt to dereference the variable. ``potential_ref`` can be either an instance of
        ``VariableRef``, in which case the return value is ``potential_ref.get(values)``, or any
        other type, in which case the ``potential_ref`` is returned. This method is a convenience
        to handle both concrete values and variable references.

        :param potential_ref: potential variable reference to dereference or concrete value
        :param values: context variable values
        :returns: the dereferences variable or the concrete value
        """
        if isinstance(potential_ref, cls):
            return potential_ref.get(values)
        return potential_ref

    @classmethod
    def try_parse(cls, value: Any) -> Any:
        """
        Attempt to parse a variable reference from a dictionary. A variable reference is defined as
        a dictionary with at least a ``variable`` key that stores the variable name to dereference.
        If the value is not a reference, return it.

        :param value: potential variable reference or concrete value
        :returns: either a ``VariableRef`` object or the concrete value
        """
        if isinstance(value, dict) and value.get('variable'):
            return cls(**value)
        return value

    @classmethod
    def parse(cls, value: Any) -> Any:
        """
        Similar to :meth:`try_parse` but raises a ``ValueError`` if the value is not a variable
        reference.

        :param value: potential variable reference or concrete value
        :returns: the variable reference object
        :raises ValueError: invalid variable reference
        """
        value = cls.try_parse(value)
        if not isinstance(value, cls):
            raise ValueError(f'invalid variable reference: {value}')
        return value


#: Registry for all available variable classes. This is populated by the
# :func:`~differ.variables.load_variables` function.
VARIABLE_TYPE_REGISTRY: dict[str, type[FuzzVariable]] = {}

#: Registry for all available comparator classes. This is populated by the
# :func:`~differ.comparators.load_comparators` function.
COMPARATOR_TYPE_REGISTRY: dict[str, type[Comparator]] = {}
