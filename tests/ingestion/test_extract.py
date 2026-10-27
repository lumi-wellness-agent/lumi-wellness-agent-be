"""app/ingestion/extract.py의 LLM 없는 부분 — 정규화, 문장 나누기, 본문 맞추기(snap), 프롬프트,
파일 형식, seed의 발췌 적용."""

from app.ingestion import extract
from app.ingestion.extract import ExcerptPick, RecordFacts, Section, SourceIndex
from app.ingestion.parse import ParsedPaper, RawItem
from app.schemas.evidence import (
    Effect,
    EvidenceMetadata,
    Medium,
    Outcome,
    OutcomeResult,
    Population,
    RecordType,
    StudyDesign,
    Verification,
)

METHODS = (
    "Participants were 114 healthy adults recruited online. They practiced for five minutes daily "
    "for one month. Cyclic sighing emphasizes prolonged exhalations."
)
RESULT_SENTENCES = [
    "All groups showed reductions in state anxiety (p < 0.001).",
    "Cyclic sighing produced greater increases in positive affect than meditation (p = 0.01).",
    "Respiratory rate decreased more in the breathwork groups.",
]
RESULTS = " ".join(RESULT_SENTENCES)
PAPER = ParsedPaper(
    "P16",
    "P16_Structured_Respiration_Practices.pdf",
    15,
    [
        RawItem("section_header", "Participants"),
        RawItem("text", METHODS, 12),
        RawItem("section_header", "Results"),
        RawItem("text", RESULTS, 3),
        RawItem("table", "| group | n |\n| sighing | 30 |", 4),
        RawItem("section_header", "References"),
        RawItem("text", "Balban, M. Y. (2023). Brief respiration.", 15),
    ],
)


def metadata() -> EvidenceMetadata:
    return EvidenceMetadata(
        record_type=RecordType.ARM,
        study_design=StudyDesign.RANDOMIZED_EXPERIMENT,
        verification=Verification.FULL_TEXT,
        arm_label="P16-E1 주기적 한숨/박스/과호흡 vs 명상",
        media=[Medium.BREATHING],
        intervention_orders=[],
        population=Population.GENERAL_ADULT,
        results=[
            OutcomeResult(
                outcome=Outcome.SUBJECTIVE_STRESS, effect=Effect.POSITIVE, measure="STAI"
            ),
            OutcomeResult(outcome=Outcome.AFFECT, effect=Effect.POSITIVE, measure="PA"),
        ],
        n=114,
    )


def facts() -> RecordFacts:
    return RecordFacts(
        key="P16-E1",
        paper_id="P16",
        experiment_id="P16-E1",
        title="Brief structured respiration practices",
        metadata=metadata(),
        summaries={
            Section.METHODS: "P16-E1 …\n대상: 일반 성인\nN: 114",
            Section.RESULTS: "결과: 모든 조건 불안↓",
        },
    )


def test_normalize_unifies_typography_and_hyphenation():
    assert extract.normalize("Respi- ration  “rate” – ﬁve") == 'respiration "rate" - five'


def test_split_sentences_keeps_sentence_boundaries():
    assert extract.split_sentences(RESULTS) == RESULT_SENTENCES


def test_snap_keeps_verbatim_sentences_and_replaces_near_misses():
    index = SourceIndex(PAPER)
    near_miss = RESULT_SENTENCES[1].replace("p = 0.01", "p=0.01")  # 공백만 다른 문장
    excerpt = " ".join(
        [RESULT_SENTENCES[0], near_miss, "The moon is made of cheese and this is invented."]
    )
    snapped = extract.snap_to_source(excerpt, index)
    assert snapped.snapped == 1 and snapped.dropped == 1
    assert "cheese" not in snapped.text
    assert "(p = 0.01)" in snapped.text  # 저장되는 문장은 본문 그대로
    assert snapped.coverage == 2 / 3 and snapped.page == 3
    assert not snapped.ok  # 짧아서 검토 대상


def test_snap_truncates_at_sentence_boundary():
    index = SourceIndex(PAPER)
    snapped = extract.snap_to_source(RESULTS, index, max_chars=120)
    assert snapped.text == RESULT_SENTENCES[0]  # 두 번째 문장을 더하면 120자를 넘는다
    assert snapped.coverage == 1.0


def test_user_prompt_carries_record_facts_and_paper_sections():
    prompt = extract.build_user_prompt(facts(), PAPER)
    assert "key: P16-E1 · 유형: ARM" in prompt
    assert "매체: BREATHING · 지표: SUBJECTIVE_STRESS, AFFECT" in prompt
    assert "시트 요약(METHODS)" in prompt and "### METHODS" in prompt and "### TABLES" in prompt
    assert "Balban, M. Y." not in prompt  # 참고문헌은 넣지 않는다


def test_budget_trims_tables_first():
    sections = {"METHODS": "m" * 100, "RESULTS": "r" * 100, "TABLES": "t" * 100}
    trimmed = extract._budget(sections, budget=250)
    assert len(trimmed["TABLES"]) == 50 and len(trimmed["RESULTS"]) == 100


def test_extract_record_uses_the_picker_and_snaps_each_section():
    def picker(system: str, user: str) -> ExcerptPick:
        assert "글자 그대로" in system and "P16-E1" in user
        return ExcerptPick(
            methods=METHODS, results=RESULTS + " Invented claim here.", confidence="HIGH"
        )

    result = extract.extract_record(facts(), PAPER, picker, source="docling P16.pdf · test")
    assert set(result.sections) == {Section.METHODS, Section.RESULTS}
    assert result.sections[Section.METHODS].coverage == 1.0
    assert result.sections[Section.METHODS].page == 12
    assert result.sections[Section.RESULTS].dropped == 1
    payload = result.to_json()
    assert payload["_confidence"] == "HIGH" and payload["METHODS"]["text"] == METHODS
    loaded = extract.load_excerpts({"P16-E1": payload, "P01": {"_source": "x"}})
    expected = {Section.METHODS: METHODS, Section.RESULTS: result.sections[Section.RESULTS].text}
    assert loaded == {"P16-E1": expected}


def test_review_reasons_flag_low_confidence_and_thin_sections():
    result = extract.RecordExcerpts(
        key="P01",
        paper_id="P01",
        experiment_id=None,
        sections={Section.RESULTS: extract.Excerpt(text="short", coverage=0.5)},
        confidence="LOW",
        source="test",
    )
    reasons = result.review_reasons
    assert reasons[0] == "confidence=LOW" and "METHODS 없음" in reasons
    assert any(r.startswith("RESULTS coverage=0.50") for r in reasons)


def test_seed_applies_excerpts_per_section_and_keeps_summaries_otherwise():
    from scripts.seed_evidence import apply_excerpts

    texts = {
        Section.METHODS: "P16-E1 …\n대상: 일반 성인",
        Section.RESULTS: "P16-E1 …\n결과: 불안↓",
    }
    merged, replaced = apply_excerpts(texts, {Section.RESULTS: RESULTS})
    assert merged == {Section.METHODS: texts[Section.METHODS], Section.RESULTS: RESULTS}
    assert replaced == [Section.RESULTS]
    assert apply_excerpts(texts, None) == (texts, [])
    only_methods, added = apply_excerpts({Section.METHODS: "요약"}, {Section.RESULTS: RESULTS})
    assert list(only_methods) == [Section.METHODS, Section.RESULTS]
    assert added == [Section.RESULTS]


def test_low_confidence_or_unverified_sections_are_not_written():
    def picker(system: str, user: str) -> ExcerptPick:
        invented = "The moon is made of cheese. Nothing here is in the paper at all."
        return ExcerptPick(methods=METHODS, results=invented, confidence="MEDIUM")

    result = extract.extract_record(facts(), PAPER, picker, source="test")
    assert set(result.sections) == {Section.METHODS}
    assert result.rejected == {Section.RESULTS: "coverage=0.00"}
    assert "RESULTS" not in result.to_json() and result.to_json()["_rejected"]
    assert any(r.startswith("RESULTS 안 씀") for r in result.review_reasons)

    def unsure(system: str, user: str) -> ExcerptPick:
        return ExcerptPick(methods=METHODS, results=RESULTS, confidence="LOW")

    low = extract.extract_record(facts(), PAPER, unsure, source="test")
    assert low.sections == {} and set(low.rejected) == {Section.METHODS, Section.RESULTS}
    assert extract.load_excerpts({"P16-E1": low.to_json()}) == {}  # 시트 요약이 남는다


def test_split_sentences_does_not_break_after_abbreviations():
    text = (
        "As shown in Fig. 3, the effect was large (Smith et al. 2019). "
        "The next one is long enough."
    )
    assert extract.split_sentences(text) == [
        "As shown in Fig. 3, the effect was large (Smith et al. 2019).",
        "The next one is long enough.",
    ]


def test_repair_glyphs_uses_context_and_corpus_words():
    vocab = {"effect": 12, "effects": 3, "difference": 5}
    glyph = extract.GLYPH
    broken = f"the e{glyph}ect and e{glyph}ects"
    assert extract.repair_glyphs(broken, vocab) == "the effect and effects"
    assert extract.repair_glyphs(f"F{glyph}2.81, p {glyph} 0.048, η 2 {glyph} .14") == (
        "F=2.81, p = 0.048, η 2 = .14"
    )
    assert extract.repair_glyphs(f"(Z = {glyph}4.18, p < 0.001)") == "(Z = \u22124.18, p < 0.001)"
    assert extract.repair_glyphs(f"aged 18{glyph} 28 years") == "aged 18– 28 years"
    unknown = f"x{glyph}yz"
    assert extract.repair_glyphs(unknown, vocab) == unknown  # 확정할 수 없으면 그대로
    assert extract.repair_glyphs("plain text") == "plain text"


def test_snap_counts_repaired_glyphs():
    glyph = extract.GLYPH
    paper = ParsedPaper(
        "P18",
        "P18.pdf",
        8,
        [
            RawItem("section_header", "Results"),
            RawItem("text", f"Groups differed in relaxation, F{glyph}2.81, p {glyph} 0.048.", 4),
        ],
    )
    index = SourceIndex(paper)
    excerpt = f"Groups differed in relaxation, F{glyph}2.81, p {glyph} 0.048."
    snapped = extract.snap_to_source(excerpt, index, repair=extract.repair_glyphs)
    assert snapped.text == "Groups differed in relaxation, F=2.81, p = 0.048."
    assert snapped.repaired == 2 and snapped.coverage == 1.0


def test_snap_finds_the_source_sentence_when_the_model_fixed_glyphs():
    glyph = extract.GLYPH
    source = (
        f"The PMR group showed a significant e{glyph}ect, F{glyph}13.40, "
        f"p{glyph}0.001 overall."
    )
    paper = ParsedPaper(
        "P18", "P18.pdf", 8, [RawItem("section_header", "Results"), RawItem("text", source, 4)]
    )
    index = SourceIndex(paper)
    model_copy = "The PMR group showed a significant effect, F=13.40, p=0.001 overall."
    snapped = extract.snap_to_source(
        model_copy, index, repair=lambda t: extract.repair_glyphs(t, {"effect": 3})
    )
    assert snapped.snapped == 1 and snapped.coverage == 1.0  # 원문 문장으로 맞췄다
    assert snapped.text == "The PMR group showed a significant effect, F=13.40, p=0.001 overall."
    assert snapped.repaired == 3


def test_snap_restores_the_full_source_sentence_when_the_model_trimmed_it():
    source = (
        "The PMR (baseline to follow-up difference=0.42, p=0.002), deep breathing (baseline to "
        "follow-up difference=0.68, p<0.001), and guided imagery groups all showed significant "
        "increases from baseline to follow-up assessment."
    )
    paper = ParsedPaper(
        "P18", "P18.pdf", 8, [RawItem("section_header", "Results"), RawItem("text", source, 4)]
    )
    trimmed = (
        "The PMR (baseline to follow-up difference=0.42, p=0.002) groups all showed significant "
        "increases from baseline to follow-up assessment."
    )
    snapped = extract.snap_to_source(trimmed, SourceIndex(paper))
    assert snapped.text == source and snapped.snapped == 1 and snapped.coverage == 1.0
    paraphrase = "Progressive muscle relaxation reliably improved how relaxed people felt overall."
    assert extract.snap_to_source(paraphrase, SourceIndex(paper)).dropped == 1  # 요약은 버린다


def test_control_characters_are_treated_as_missing_glyphs():
    nul = chr(0)
    source = f"The average daily change in state anxiety was {nul}3.95 for mindfulness meditation."
    paper = ParsedPaper(
        "P16", "P16.pdf", 15, [RawItem("section_header", "Results"), RawItem("text", source, 3)]
    )
    model_copy = "The average daily change in state anxiety was -3.95 for mindfulness meditation."
    snapped = extract.snap_to_source(model_copy, SourceIndex(paper), repair=extract.repair_glyphs)
    expected = (
        "The average daily change in state anxiety was \u22123.95 for mindfulness meditation."
    )
    assert snapped.text == expected
    assert snapped.repaired == 1


def test_old_elsevier_font_mapping_is_repaired_and_matched():
    eth, thorn, quarter, glyph = "\u00f0", "\u00de", "\u00bc", extract.GLYPH
    source = (
        f"Nonsignificant effects were found for RT, t{eth}15{thorn} {quarter} {glyph}:91, "
        f"p {quarter} :37."
    )
    assert extract.repair_glyphs(source) == (
        "Nonsignificant effects were found for RT, t(15) = \u2212.91, p = .37."
    )
    paper = ParsedPaper(
        "P03", "P03.pdf", 11, [RawItem("section_header", "Results"), RawItem("text", source, 6)]
    )
    model_copy = "Nonsignificant effects were found for RT, t(15) = -.91, p = .37."
    snapped = extract.snap_to_source(model_copy, SourceIndex(paper), repair=extract.repair_glyphs)
    assert snapped.text == "Nonsignificant effects were found for RT, t(15) = \u2212.91, p = .37."
    assert snapped.repaired == 5 and snapped.coverage == 1.0


def test_split_sentences_handles_the_old_font_period_after_statistics():
    text = (
        "There was a significant interaction, F(3, 78) = 8:46; po0:001: As Fig. 3 shows, "
        "the nature group had longer IBI, F(1, 26) = 11:40; po0:01: For the nature group "
        "IBI increased during the video."
    )
    assert extract.split_sentences(text) == [
        "There was a significant interaction, F(3, 78) = 8:46; po0:001:",
        "As Fig. 3 shows, the nature group had longer IBI, F(1, 26) = 11:40; po0:01:",
        "For the nature group IBI increased during the video.",
    ]


def test_old_font_equals_after_an_abbreviation():
    quarter = "\u00bc"
    assert extract.repair_glyphs(f"(mean age = 23, S.D. {quarter} 3.22)") == (
        "(mean age = 23, S.D. = 3.22)"
    )
