import pytest

from herdr_discord_bridge.models import PendingQuestion, QuestionOption
from herdr_discord_bridge.questions import parse_question_answer, selection_keys


def question(*, multi_select=False):
    return PendingQuestion(
        pane_id="w1:p1",
        event_id="question-1",
        prompt="Choose one",
        options=(
            QuestionOption("One"),
            QuestionOption("Two"),
            QuestionOption("Three"),
        ),
        multi_select=multi_select,
    )


def test_numeric_answer_selects_one_based_option():
    assert parse_question_answer("2", question()) == (1,)
    assert parse_question_answer("1, 3", question()) is None
    assert parse_question_answer("4", question()) is None


def test_numeric_answer_accepts_multiple_choices_for_multi_select():
    assert parse_question_answer("3 1", question(multi_select=True)) == (0, 2)


def test_selection_keys_moves_to_correct_single_choice():
    assert selection_keys(question(), (2,)) == ("Home", "Down", "Down", "Enter")


def test_selection_keys_toggles_multiple_choices_before_confirming():
    assert selection_keys(question(multi_select=True), (0, 2)) == (
        "Home",
        "Space",
        "Down",
        "Down",
        "Space",
        "Enter",
    )


def test_selection_keys_rejects_invalid_single_selection():
    with pytest.raises(ValueError, match="exactly one"):
        selection_keys(question(), (0, 1))
