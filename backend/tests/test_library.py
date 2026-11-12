import pytest

from app.retrieval.library import LibraryStats, library_answer, library_question


@pytest.mark.parametrize(
    ("question", "kind"),
    [
        ("how many docs are indexed", "count"),
        ("How many documents are there?", "count"),
        ("How many files do you have access to?", "count"),
        ("What's the total number of documents in the library?", "count"),
        ("Which documents do you have?", "list"),
        ("What files have been uploaded?", "list"),
        ("List all the documents", "list"),
        ("show me the docs", "list"),
        ("What documents are available?", "list"),
    ],
)
def test_recognises_questions_about_the_collection(question: str, kind: str) -> None:
    assert library_question(question) == kind


@pytest.mark.parametrize(
    "question",
    [
        # Questions about the documents' contents stay with the model.
        "How many documents must I submit with an expense claim?",
        "Which documents mention parking?",
        "What is this document about?",
        "What documents do I need for onboarding?",
        "How many days of leave do I get?",
        # A count of one kind of file would be answered with the count of all.
        "How many PDFs are indexed?",
        "Summarise this doc",
        "Which document covers passwords?",
    ],
)
def test_leaves_other_questions_alone(question: str) -> None:
    assert library_question(question) is None


def test_count_answer() -> None:
    stats = LibraryStats(indexed=13, in_progress=2, failed=1, names=["a.pdf"])
    text = library_answer(stats, "count", scoped=False)
    assert text.startswith("**13 documents** are indexed and available to you.")
    assert "2 more documents are waiting to be indexed" in text
    assert "1 document could not be indexed." in text
    assert "a.pdf" not in text  # a count doesn't list names


def test_list_answer_names_documents_and_says_when_cut_short() -> None:
    stats = LibraryStats(indexed=3, in_progress=0, failed=0, names=["a.pdf", "b.docx"])
    text = library_answer(stats, "list", scoped=True)
    assert text.startswith("**3 documents** in the current scope are indexed")
    assert "- a.pdf\n- b.docx" in text
    assert "…and 1 more." in text


def test_singular_and_empty() -> None:
    one = library_answer(LibraryStats(1, 0, 0, ["a.pdf"]), "count", scoped=False)
    assert one.startswith("**1 document** is indexed")
    none = library_answer(LibraryStats(0, 1, 0, []), "list", scoped=False)
    assert none.startswith("No documents are indexed and available to you yet.")
    assert "1 more document is waiting" in none
