from pathlib import Path

import app.prompts as prompts


def test_duplicate_demo_trigger_module_removed() -> None:
    assert not Path("app/demo_trigger.py").exists()


def test_stale_ui_verification_prompt_removed() -> None:
    assert not hasattr(prompts, "ui_verification_prompt")
