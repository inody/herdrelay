from unittest.mock import Mock

import pytest

from herdrelay.config import AppConfig
from herdrelay.discord_bot import HerdrDiscordBot, QuestionChoiceView
from herdrelay.herdr_client import HerdrClient
from herdrelay.models import HerdrTarget, PendingQuestion, QuestionOption
from herdrelay.store import Store


def question(*, multi_select=False, event_id="question-1"):
    return PendingQuestion(
        pane_id="w1:p1",
        event_id=event_id,
        prompt="Choose",
        options=(QuestionOption("One"), QuestionOption("Two"), QuestionOption("Three")),
        multi_select=multi_select,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("multi_select", [False, True])
async def test_question_view_never_expires_and_can_be_reregistered(multi_select):
    view = QuestionChoiceView(bot=None, question=question(multi_select=multi_select))  # type: ignore[arg-type]
    again = QuestionChoiceView(bot=None, question=question(multi_select=multi_select))  # type: ignore[arg-type]

    assert view.timeout is None
    assert view.is_persistent()
    assert [item.custom_id for item in view.children] == [
        item.custom_id for item in again.children
    ]


@pytest.mark.asyncio
async def test_question_views_for_different_questions_do_not_share_ids():
    first = QuestionChoiceView(bot=None, question=question(event_id="a"))  # type: ignore[arg-type]
    second = QuestionChoiceView(bot=None, question=question(event_id="b"))  # type: ignore[arg-type]

    first_ids = {item.custom_id for item in first.children}
    second_ids = {item.custom_id for item in second.children}
    assert not first_ids & second_ids


@pytest.mark.asyncio
async def test_structured_question_answer_uses_backend_operation_and_clears_state(tmp_path):
    pending = question()
    store = Store(tmp_path / "store.sqlite3")
    store.upsert_pending_question(pending)
    client = Mock(spec=HerdrClient)
    client.resolve_target.return_value = HerdrTarget(
        target=pending.pane_id, agent_name="claude", status="blocked"
    )
    bot = HerdrDiscordBot(
        config=AppConfig(discord_token="token", enable_questions=True),
        store=store,
        client=client,
    )

    labels = await bot._answer_pending_question(pending, (1,))

    assert labels == "Two"
    client.answer_question.assert_called_once_with(pending, (1,))
    assert store.get_pending_question(pending.pane_id) is None


@pytest.mark.asyncio
async def test_failed_structured_question_answer_restores_pending_state(tmp_path):
    pending = question()
    store = Store(tmp_path / "store.sqlite3")
    store.upsert_pending_question(pending)
    client = Mock(spec=HerdrClient)
    client.resolve_target.return_value = HerdrTarget(
        target=pending.pane_id, agent_name="claude", status="blocked"
    )
    client.answer_question.side_effect = RuntimeError("input failed")
    bot = HerdrDiscordBot(
        config=AppConfig(discord_token="token", enable_questions=True),
        store=store,
        client=client,
    )

    with pytest.raises(RuntimeError, match="input failed"):
        await bot._answer_pending_question(pending, (0,))

    assert store.get_pending_question(pending.pane_id) == pending
