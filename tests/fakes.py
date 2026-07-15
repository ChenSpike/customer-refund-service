"""
Minimal fakes for the Azure OpenAI Responses API, shaped to exactly what
triage_node consumes: items with .type, function-call .arguments/.call_id,
and message parts with .text. Lets the 20 triage tests run offline and
deterministically.
"""
import json
from dataclasses import dataclass, field


@dataclass
class FakeFunctionCall:
    arguments: str
    call_id: str = "call_1"
    type: str = "function_call"
    name: str = "Order_Database_Lookup"


@dataclass
class FakeTextPart:
    text: str


@dataclass
class FakeMessage:
    content: list
    type: str = "message"


@dataclass
class FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class FakeResponse:
    output: list = field(default_factory=list)
    usage: FakeUsage | None = None


class FakeClient:
    """
    Scripted stand-in for the module-level AzureOpenAI client.
    Each responses.create() call pops the next scripted FakeResponse and
    records the kwargs it was called with (for asserting on `input`).
    """

    def __init__(self, scripted: list[FakeResponse]):
        self._scripted = list(scripted)
        self.calls: list[dict] = []
        self.responses = self  # so client.responses.create(...) resolves here

    def create(self, **kwargs) -> FakeResponse:
        self.calls.append(kwargs)
        if not self._scripted:
            raise AssertionError("FakeClient ran out of scripted responses")
        return self._scripted.pop(0)


def tool_call_response(order_id: str, usage: FakeUsage | None = None) -> FakeResponse:
    return FakeResponse(output=[
        FakeFunctionCall(arguments=json.dumps({"order_id": order_id}))
    ], usage=usage)


def text_response(text: str, usage: FakeUsage | None = None) -> FakeResponse:
    return FakeResponse(output=[FakeMessage(content=[FakeTextPart(text=text)])],
                        usage=usage)


def classification_response(reason_or_payload, usage: FakeUsage | None = None) -> FakeResponse:
    """Build the second-turn JSON classification reply.

    Accepts a reason string, or a raw dict for malformed-payload tests.
    """
    payload = (
        {"refund_reason": reason_or_payload}
        if isinstance(reason_or_payload, str)
        else reason_or_payload
    )
    return text_response(json.dumps(payload), usage=usage)
