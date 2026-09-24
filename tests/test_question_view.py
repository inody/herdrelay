import pytest

from herdr_discord_bridge.discord_bot import QuestionChoiceView
from herdr_discord_bridge.models import PendingQuestion, QuestionOption


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
