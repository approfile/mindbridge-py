"""One output contract for intent prompting, parsing and evaluation."""
import json

from pydantic import BaseModel, ConfigDict

from app.core.enums import IntentType

INTENT_CONTRACT = 'intent-json-v1'
INTENT_MARKER = 'INTENT_CLASSIFICATION'


class IntentPrediction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    intent: IntentType


def parse_intent(raw: str) -> IntentType:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate intent JSON key')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('non-finite JSON value')
    value = json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    return IntentPrediction.model_validate(value).intent
