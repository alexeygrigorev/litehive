from litehive.tasks import slugify


def test_short_slug_unchanged():
    assert slugify("fix login bug") == "fix-login-bug"


def test_slugify_truncates_at_50_chars():
    title = "this is a very long task title that should definitely be truncated at fifty characters or fewer"
    result = slugify(title)
    assert len(result) <= 50
    assert not result.endswith("-")


def test_exactly_50_chars_unchanged():
    # Build a slug that is exactly 50 characters
    slug_input = "a-" * 24 + "ab"  # produces "a-a-a-...-ab" = 50 chars after slugify
    result = slugify(slug_input)
    assert len(result) == 50


def test_word_boundary_trimming():
    # 51 chars after slugify: should truncate at last hyphen within 50
    title = "abcdefghij " * 5 + "x"
    result = slugify(title)
    assert len(result) <= 50
    assert not result.endswith("-")


def test_empty_and_special_chars():
    assert slugify("") == "task"
    assert slugify("!!!") == "task"
    assert slugify("@#$%") == "task"
