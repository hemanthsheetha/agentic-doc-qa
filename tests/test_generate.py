from app.generate import _figures_cited_by, _snippet

ANSWER = ("Uber's total revenue in 2021 was $17,455 million [2]. "
          "That was up from $11,139 million in 2020 [3]. Gross bookings grew 56% [2][4].")


def test_figures_are_taken_only_from_sentences_citing_the_source():
    assert _figures_cited_by(ANSWER, 2) == ["17,455", "56"]   # longest first; years dropped
    assert _figures_cited_by(ANSWER, 3) == ["11,139"]
    assert _figures_cited_by(ANSWER, 4) == ["56"]
    assert _figures_cited_by(ANSWER, 9) == []


def test_uncited_sentence_is_backed_by_next_citation():
    answer = "Revenue in 2021 was $17,455 million. This was 57% above 2020's $11,139 million [1]."
    assert _figures_cited_by(answer, 1) == ["11,139", "17,455", "57"]


def test_table_rows_are_backed_by_their_own_citation():
    answer = ("| Period | Revenue | Source |\n|---|---|---|\n"
              "| Q1 2022 | $6,854M | [1] |\n| Q2 2022 | $8,073M | [2] |")
    assert _figures_cited_by(answer, 1) == ["6,854"]
    assert _figures_cited_by(answer, 2) == ["8,073"]


def test_snippet_centers_on_cited_figure():
    text = "intro " * 200 + "Total Revenue $ 13,000 $ 11,139 $ 17,455 As reported" + " outro" * 200
    snippet, found = _snippet(text, ["17,455", "999,999"])
    assert "17,455" in snippet and found == ["17,455"]
    assert snippet.startswith("…") and snippet.endswith("…")
    assert len(snippet) < 600


def test_snippet_falls_back_to_chunk_start():
    snippet, found = _snippet("short chunk without the figure", ["17,455"])
    assert snippet == "short chunk without the figure" and found == []
