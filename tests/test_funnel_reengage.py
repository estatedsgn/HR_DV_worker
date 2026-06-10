from datetime import UTC, datetime, timedelta

from app.services.funnel_graph.reengage import (
    COLD_TOUCH_TEXTS,
    EXCLUDED_STAGES,
    WARM_FINAL_TEXTS,
    next_touch_number,
    parse_touch_delays,
    touch_text,
)


NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
DELAYS = parse_touch_delays("4,20,48")


def test_parse_touch_delays_defaults_on_garbage() -> None:
    assert parse_touch_delays("") == [timedelta(hours=4), timedelta(hours=20), timedelta(hours=48)]
    assert parse_touch_delays("abc,,") == [timedelta(hours=4), timedelta(hours=20), timedelta(hours=48)]
    assert parse_touch_delays("1,2") == [timedelta(hours=1), timedelta(hours=2)]


def test_no_touch_before_first_delay() -> None:
    last = NOW - timedelta(hours=3, minutes=59)
    assert next_touch_number(touch_count=0, last_message_at=last, now=NOW, delays=DELAYS) is None


def test_first_touch_after_four_hours() -> None:
    last = NOW - timedelta(hours=4, minutes=1)
    assert next_touch_number(touch_count=0, last_message_at=last, now=NOW, delays=DELAYS) == 1


def test_second_touch_needs_longer_pause() -> None:
    last = NOW - timedelta(hours=5)
    # После бампа №1 пауза 20 часов — 5 часов мало.
    assert next_touch_number(touch_count=1, last_message_at=last, now=NOW, delays=DELAYS) is None
    last = NOW - timedelta(hours=21)
    assert next_touch_number(touch_count=1, last_message_at=last, now=NOW, delays=DELAYS) == 2


def test_no_touch_after_ladder_exhausted() -> None:
    last = NOW - timedelta(days=30)
    assert next_touch_number(touch_count=3, last_message_at=last, now=NOW, delays=DELAYS) is None


def test_naive_datetime_treated_as_utc() -> None:
    last = (NOW - timedelta(hours=5)).replace(tzinfo=None)
    assert next_touch_number(touch_count=0, last_message_at=last, now=NOW, delays=DELAYS) == 1


def test_cold_texts_differ_between_touches() -> None:
    texts = [
        touch_text(touch_number=n, cold=True, stage="interest_check", dialog_key="d1", touch_count_total=3)
        for n in (1, 2, 3)
    ]
    assert len(set(texts)) == 3
    assert texts[0] in COLD_TOUCH_TEXTS[0]
    assert texts[2] in COLD_TOUCH_TEXTS[2]


def test_warm_touch_uses_stage_question_and_final_backs_off() -> None:
    from app.services.funnel_graph.funnel_policy import get_stage_policy
    from app.services.funnel_graph.reply import question_variants

    t1 = touch_text(
        touch_number=1, cold=False, stage="post_equipment_questions_check", dialog_key="d2", touch_count_total=3
    )
    # Тёплый бамп возвращает к цели стадии (переформулировка вопроса), не к опенеру.
    stage_question = get_stage_policy("post_equipment_questions_check").current_question
    assert any(variant in t1 for variant in question_variants(stage_question))
    final = touch_text(
        touch_number=3, cold=False, stage="post_equipment_questions_check", dialog_key="d2", touch_count_total=3
    )
    assert final in WARM_FINAL_TEXTS


def test_warm_touches_rotate_variants_no_verbatim_repeat() -> None:
    t1 = touch_text(touch_number=1, cold=False, stage="interview_offer", dialog_key="d3", touch_count_total=3)
    t2 = touch_text(touch_number=2, cold=False, stage="interview_offer", dialog_key="d3", touch_count_total=3)
    assert t1 != t2


def test_different_dialogs_get_different_variants() -> None:
    pool = {
        touch_text(touch_number=1, cold=True, stage="interest_check", dialog_key=f"d{i}", touch_count_total=3)
        for i in range(8)
    }
    assert len(pool) > 1  # ротация по диалогу работает


def test_excluded_stages_cover_terminal_and_scheduled() -> None:
    for stage in ("human_handoff", "lost", "do_not_contact", "ready_for_interview", "scheduled_until_18"):
        assert stage in EXCLUDED_STAGES
    assert "interest_check" not in EXCLUDED_STAGES
    assert "age_pending_18" not in EXCLUDED_STAGES
