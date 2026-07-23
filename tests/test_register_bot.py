import ast
from pathlib import Path


def test_bot_lifecycle_uses_only_v2_methods_and_unregisters_with_token():
    source = (Path(__file__).parents[1] / "server-side/register_bot.py").read_text(encoding="utf-8")

    assert '"imbot.v2.Bot.register"' in source
    assert '"botToken": new_token' in source
    assert '"name": bot_name' in source
    assert "imbot.bot.list" not in source
    assert "imbot.unregister" not in source


def test_registration_payload_puts_bot_token_inside_fields():
    source = (Path(__file__).parents[1] / "server-side/register_bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    payload = next(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "reg_payload" for target in node.targets)
    )
    assert isinstance(payload, ast.Dict)
    root_keys = [key.value for key in payload.keys if isinstance(key, ast.Constant)]
    fields = next(
        value for key, value in zip(payload.keys, payload.values) if isinstance(key, ast.Constant) and key.value == "fields"
    )
    assert isinstance(fields, ast.Dict)
    field_keys = [key.value for key in fields.keys if isinstance(key, ast.Constant)]

    assert "botToken" not in root_keys
    assert "botToken" in field_keys
