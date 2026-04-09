from litehive.tasks import slugify


def test_slugify_short_unchanged() -> None:
    assert slugify("Fix login race") == "fix-login-race"


def test_slugify_truncates_at_50_chars() -> None:
    title = "this is a very long task title that should be truncated to fifty characters or fewer"
    result = slugify(title)
    assert len(result) <= 50
    assert not result.endswith("-")
    assert result == "this-is-a-very-long-task-title-that-should-be"


def test_slugify_truncation_word_boundary() -> None:
    # "a-" * 25 + "b" -> after slugify: "a-a-a-...-a-b" = 51 chars
    title = "a-" * 25 + "b"
    result = slugify(title)
    assert len(result) <= 50
    assert not result.endswith("-")


def test_slugify_empty_gives_task() -> None:
    assert slugify("") == "task"
    assert slugify("!!!") == "task"
    assert slugify("@#$%") == "task"
