"""
Hand crafted classes which should undoubtedly be autogenerated from the schema.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Protocol, TypeVar, cast
import re

try:
    from typing import dataclass_transform
except ImportError:
    from typing_extensions import dataclass_transform

import json

from attrs import asdict, field, frozen
from referencing import Registry, Specification
from referencing.jsonschema import Schema, SchemaRegistry, specification_with

from bowtie import exceptions

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping

    from bowtie._core import DialectRunner
    from bowtie._report import CaseReporter


Seq = int


@frozen
class Test:
    description: str
    instance: Any
    comment: str | None = None
    valid: bool | None = None


@frozen
class TestCase:
    description: str
    schema: Any
    tests: list[Test]
    comment: str | None = None
    registry: SchemaRegistry = Registry()

    @classmethod
    def from_dict(
        cls,
        dialect: str | None,
        tests: Iterable[dict[str, Any]],
        registry: Mapping[str, Schema] = {},
        **kwargs: Any,
    ) -> TestCase:
        populated: SchemaRegistry = Registry().with_contents(  # type: ignore[reportUnknownMemberType]
            registry.items(),
            default_specification=specification_with(
                dialect or "urn:bowtie:unknown-dialect",
                default=Specification.OPAQUE,
            ),
        )
        return cls(
            tests=[Test(**test) for test in tests],
            registry=populated,
            **kwargs,
        )

    def run(
        self,
        seq: Seq,
        runner: DialectRunner,
    ) -> Awaitable[ReportableResult]:
        command = Run(seq=seq, case=self.without_expected_results())
        return runner.run_validation(command=command, tests=self.tests)

    def serializable(self) -> dict[str, Any]:
        as_dict = asdict(
            self,
            filter=lambda k, v: k.name != "registry"
            and (k.name != "comment" or v is not None),
        )
        if self.registry:
            # FIXME: Via python-jsonschema/referencing#16
            as_dict["registry"] = {
                k: v.contents for k, v in self.registry.items()
            }
        return as_dict

    def without_expected_results(self) -> dict[str, Any]:
        serializable = self.serializable()
        serializable["tests"] = [
            {
                k: v
                for k, v in test.items()
                if k != "valid" and (k != "comment" or v is not None)
            }
            for test in serializable.pop("tests")
        ]
        return serializable


@frozen
class Started:
    implementation: dict[str, Any]
    ready: bool = field()
    version: int = field(
        validator=lambda _, __, got: exceptions.VersionMismatch.check(got),
    )

    @ready.validator  # type: ignore[reportGeneralTypeIssues]
    def _check_ready(self, _: Any, ready: bool):
        if not ready:
            raise exceptions.ImplementationNotReady()


R = TypeVar("R", covariant=True)


class Command(Protocol[R]):
    def to_request(self, validate: Callable[..., None]) -> dict[str, Any]:
        ...

    @staticmethod
    def from_response(
        response: bytes,
        validate: Callable[..., None],
    ) -> R | None:
        ...


@dataclass_transform()
def command(
    Response: Callable[..., R | None],
    name: str = "",
) -> Callable[[type], type[Command[R]]]:
    def _command(cls: type) -> type[Command[R]]:
        nonlocal name
        if not name:
            name = re.sub(r"([a-z])([A-Z])", r"\1-\2", cls.__name__).lower()

        uri = f"https://bowtie.report/schemas/io/commands/{name}/"
        request_schema = {"$ref": uri}
        response_schema = {"$ref": f"{uri}response/"}

        def to_request(
            self: Command[R],
            validate: Callable[..., None],
        ) -> dict[str, Any]:
            request = dict(cmd=name, **asdict(self))
            validate(instance=request, schema=request_schema)
            return request

        @staticmethod
        def from_response(
            response: bytes,
            validate: Callable[..., None],
        ) -> R | None:
            try:
                instance = json.loads(response)
            except json.JSONDecodeError as error:
                raise exceptions._ProtocolError(errors=[error])  # type: ignore[reportPrivateUsage]
            validate(instance=instance, schema=response_schema)
            return Response(**instance)

        cls = cast(type[Command[R]], cls)
        cls.to_request = to_request
        cls.from_response = from_response
        return frozen(cls)

    return _command


@command(Response=Started)
class Start:
    version: int


START_V1 = Start(version=1)


@frozen
class StartedDialect:
    ok: bool

    OK: ClassVar[StartedDialect]


StartedDialect.OK = StartedDialect(ok=True)


@command(Response=StartedDialect)
class Dialect:
    dialect: str


def _case_result(
    errored: bool = False,
    skipped: bool = False,
    **response: Any,
) -> Callable[[str, bool | None], CaseResult | CaseSkipped | CaseErrored]:
    if errored:
        return lambda implementation, expected: CaseErrored(
            implementation=implementation,
            **response,
        )
    elif skipped:
        return lambda implementation, expected: CaseSkipped(
            implementation=implementation,
            **response,
        )
    return lambda implementation, expected: CaseResult.from_dict(
        response,
        implementation=implementation,
        expected=expected,
    )


@frozen
class TestResult:
    errored = False
    skipped = False

    valid: bool

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
    ) -> TestResult | SkippedTest | ErroredTest:
        if data.pop("skipped", False):
            return SkippedTest(**data)
        elif data.pop("errored", False):
            return ErroredTest(**data)
        return cls(valid=data["valid"])


@frozen
class SkippedTest:
    message: str | None = field(default=None)
    issue_url: str | None = field(default=None)

    errored = False
    skipped: bool = field(init=False, default=True)

    @property
    def reason(self) -> str:
        if self.message is not None:
            return self.message
        if self.issue_url is not None:
            return self.issue_url
        return "skipped"


@frozen
class ErroredTest:
    context: dict[str, Any] = field(factory=dict)

    errored: bool = field(init=False, default=True)
    skipped: bool = False

    @property
    def reason(self) -> str:
        message = self.context.get("message")
        if message:
            return message
        return "Encountered an error."


class ReportableResult(Protocol):
    errored: bool
    failed: bool

    @property
    def implementation(self) -> str:
        ...

    def report(self, reporter: CaseReporter) -> None:
        pass


@frozen
class CaseResult:
    errored = False

    implementation: str
    seq: Seq
    results: list[TestResult | SkippedTest | ErroredTest]
    expected: list[bool | None]

    @classmethod
    def from_dict(cls, data: Any, **kwargs: Any) -> CaseResult:
        results = [TestResult.from_dict(t) for t in data.pop("results")]
        return cls(results=results, **data, **kwargs)

    @property
    def failed(self) -> bool:
        return any(failed for _, failed in self.compare())

    def report(self, reporter: CaseReporter) -> None:
        reporter.got_results(self)

    def compare(
        self,
    ) -> Iterable[tuple[TestResult | SkippedTest | ErroredTest, bool]]:
        for test, expected in zip(self.results, self.expected):
            failed: bool = (  # type: ignore[reportUnknownVariableType]
                not test.skipped
                and not test.errored
                and expected is not None
                and expected != test.valid  # type: ignore[reportUnknownMemberType]
            )
            yield test, failed


@frozen
class CaseErrored:
    """
    A full test case errored.
    """

    errored = True
    failed = False

    implementation: str
    seq: Seq
    context: dict[str, Any]

    caught: bool = True

    def report(self, reporter: CaseReporter):
        reporter.case_errored(self)

    @classmethod
    def uncaught(
        cls,
        implementation: str,
        seq: Seq,
        **context: Any,
    ) -> CaseErrored:
        return cls(
            implementation=implementation,
            seq=seq,
            caught=False,
            context=context,
        )


@frozen
class CaseSkipped:
    """
    A full test case was skipped.
    """

    errored = False
    failed = False

    implementation: str
    seq: Seq

    message: str | None = None
    issue_url: str | None = None
    skipped: bool = field(init=False, default=True)

    def report(self, reporter: CaseReporter):
        reporter.skipped(self)


@frozen
class Empty:
    """
    An implementation didn't send a response.
    """

    errored = True
    failed = False

    implementation: str

    def report(self, reporter: CaseReporter):
        reporter.no_response(implementation=self.implementation)


@command(Response=_case_result)
class Run:
    seq: Seq
    case: dict[str, Any]


@command(Response=lambda: None)
class Stop:
    pass


STOP = Stop()
