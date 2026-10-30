"""scripts/rag_compare.py의 LLM · 네트워크 없는 부분 — 인용 찾기(두 표기), 인용 목록에 잇기, 찾은
논문 대조, 출처 유형, 학술 주소, llm · web 입력 만들기, 집계. 생성 · 실존 확인 · Judge는
`make rag-compare`로 따로 돌린다.
"""

from scripts import rag_compare

EXPLAIN_INPUT = """\
## 사용자 입력
내일 면접에서 실수할 장면만 자꾸 떠올라요

## 사용자 상태
- 목표: S1 — 반복되는 생각에서 벗어나기(반추 완화)
- 사용자 상태: 스트레스 상황을 앞둠(PRE_STRESS)
- 쓸 수 있는 시간: 20분
- 장소: INDOOR
- 제약: 없음
- 이미지 제공: AI 생성 이미지(연구 자극과 다르다 — ADAPTED_STIMULUS)
- 요약: -

## 고른 활동
- PR-09 3분 안내 마음챙김 — 구성 SINGLE, 목표 지표 등급: 반추 B
- 고른 이유: rank 1인 PR-09을 선택합니다.

## 검증된 근거 (1건)
- [54] Bolzenkötter, Neubauer & Koval (2025) — Impact … · 목표 지지: 예

## 꼭 밝힐 것
없음

## 콘텐츠 Factor (고를 칸)
이 활동은 고를 칸이 없다."""


def test_compare_input_keeps_user_state_and_activity_name_only():
    text = rag_compare.compare_input({"explain_input": EXPLAIN_INPUT})
    assert "내일 면접에서" in text and "PRE_STRESS" in text
    assert "- PR-09 3분 안내 마음챙김" in text
    # 근거 · 꼭 밝힐 것 · Factor · 자극 변형 고지 · 선택 호출의 이유는 llm · web에 주지 않는다
    hidden = ("검증된 근거", "꼭 밝힐 것", "콘텐츠 Factor", "이미지 제공", "고른 이유", "등급")
    for leaked in hidden:
        assert leaked not in text


def test_find_mentions_reads_both_citation_styles():
    text = (
        "면접을 앞둔 상태라 마음챙김을 골랐습니다. Bolzenkötter, Neubauer & Koval (2025)은 반추가"
        " 줄었다고 했고, Macrynikola et al. (2024)도 같은 결과입니다. 앱 연구에서도 반추 개선이"
        " 확인됐습니다(Curry & Kasser, 2005; Holt, Furbert & Sweetingham, 2019; 개선 지표 없음)."
        " 여러 연구에서 비슷한 결과가 보고됐습니다. 다만 연구에서 쓴 자극과 다릅니다."
    )
    mentions, unattributed = rag_compare.find_mentions(text)
    assert [(m["surnames"][0], m["year"]) for m in mentions] == [
        ("Bolzenkötter", 2025), ("Macrynikola", 2024), ("Curry", 2005), ("Holt", 2019),
    ]
    assert mentions[0]["surnames"] == ["Bolzenkötter", "Neubauer", "Koval"]
    assert mentions[1]["sentence"].startswith("Bolzenkötter")
    assert mentions[3]["sentence"].startswith("앱 연구에서도")
    # '(저자, 연도)' 괄호 안의 연도 아닌 조각은 인용이 아니고, '다만 …' 한계 고지는 주장이 아니다
    assert unattributed == ["여러 연구에서 비슷한 결과가 보고됐습니다."]


def test_find_mentions_handles_nested_web_style_and_korean_particles():
    text = (
        "효과가 확인되었고(Jain et al. (2007)), Kim과 Lee(2019)도 같습니다."
        " Smith 등(2020)은 다릅니다."
    )
    mentions, _ = rag_compare.find_mentions(text)
    assert [(m["surnames"], m["year"]) for m in mentions] == [
        (["Jain"], 2007), (["Kim", "Lee"], 2019), (["Smith"], 2020),
    ]


def test_resolve_links_mention_to_declared_citation_by_year_and_surname():
    citations = [
        {"authors": "Ulrich et al.", "year": 1991, "title": "Stress recovery"},
        {"authors": "Berto", "year": 2005, "title": "Exposure to restorative environments"},
        {"authors": "Hartig et al.", "year": 2005, "title": "Other"},
    ]
    assert rag_compare.resolve(
        {"surnames": ["Berto"], "year": 2005}, citations
    )["title"] == "Exposure to restorative environments"
    # 같은 연도가 둘이고 성이 안 맞으면 잇지 않는다 — UNDECLARED로 센다
    assert rag_compare.resolve({"surnames": ["Kaplan"], "year": 2005}, citations) is None
    # 성 없이 연도만 있어도 그 연도 인용이 하나뿐이면 그것이다
    assert rag_compare.resolve({"surnames": [], "year": 1991}, citations)["authors"] == (
        "Ulrich et al."
    )


def test_check_compares_title_year_and_first_author():
    record = {"title": "Exposure to restorative environments helps restore attentional capacity",
              "year": 2005, "authors": ["Rita Berto"]}
    mention = {"surnames": ["Berto"], "year": 2005, "authors": "Berto"}
    check = rag_compare._check(record, mention, "Exposure to restorative environments helps "
                               "restore attentional capacity")
    assert check["year_ok"] and check["author_ok"] and check["similarity"] == 1.0
    guo = {"surnames": ["Guo"], "year": 2008, "authors": "Guo"}
    assert rag_compare._check(record, guo, "")["problems"] == [
        "연도 2005 ≠ 2008", "저자 Rita Berto ≠ Guo",
    ]
    # 붙임표 · 아포스트로피 성은 낱말로 나눠 모두 있으면 맞다
    cooper = {"title": "x", "year": 2024,
              "authors": ["Emma Claire Palmer-Cooper", "R. Seneviratne"]}
    assert rag_compare._check(cooper, {"surnames": ["Palmer-Cooper"], "year": 2024,
                                       "authors": "Palmer-Cooper et al."}, "")["author_ok"]
    oconnell = {"title": "x", "year": 2017, "authors": ["Brenda H. O’Connell"]}
    assert rag_compare._check(oconnell, {"surnames": ["O’Connell"], "year": 2017,
                                         "authors": "O’Connell et al."}, "")["author_ok"]
    # 성은 낱말 단위다 — 'La'가 'Lapsley' 안에 있다고 맞는 것이 아니고, 세 글자 미만은 보지 않는다
    lapsley = {"title": "x", "year": 2005, "authors": ["Mark Lapsley"]}
    assert not rag_compare._check(lapsley, {"surnames": ["La", "Torre"], "year": 2005,
                                            "authors": "La Torre"}, "")["author_ok"]
    # 저자가 없는 레코드(책)는 저자로 반증하지 않는다
    book = {"title": "x", "year": 1989, "authors": ["No authorship indicated"]}
    assert rag_compare._check(book, {"surnames": ["Kaplan"], "year": 1989,
                                     "authors": "Kaplan & Kaplan"}, "")["author_ok"]
    # 악센트 · 대소문자는 무시한다
    assert rag_compare.fold("Bolzenkötter") == "bolzenkotter"


def test_author_title_overlap_rejects_same_template_different_topic():
    # 저자 · 연도만 같은 다른 논문 — 'The effect of … : A meta-analysis' 틀만 같다
    assert not rag_compare.author_title_ok(
        "The effect of viewing nature on stress and anxiety: A meta-analysis",
        "The effect of HLA on immunological response to hepatitis B vaccine: A meta-analysis",
    )
    # 제목을 틀리게 낸 같은 논문 — 내용 낱말이 겹친다
    assert rag_compare.author_title_ok(
        "Can coloring help? Adult coloring and anxiety",
        "Can Coloring Mandalas Reduce Anxiety? A Replication Study",
    )
    assert rag_compare.author_title_ok(
        "Exposure to restorative sounds, and the restorative potential of the environment",
        "Exposure to restorative environments helps restore attentional capacity",
    )


def test_source_type_and_academic_ident():
    assert rag_compare.source_type("https://pubmed.ncbi.nlm.nih.gov/17291166/") == "학술"
    assert rag_compare.source_type("https://doi.org/10.1007/s42761-024-00291-9") == "학술"
    assert rag_compare.source_type("https://www.frontiersin.org/articles/10.3389/x") == "학술"
    assert rag_compare.source_type("https://blog.naver.com/abc/123") == "블로그"
    assert rag_compare.source_type("https://www.healthline.com/health/breathing") == "언론"
    assert rag_compare.source_type("https://www.nccih.nih.gov/health/meditation") == "공공"
    assert rag_compare.source_type("https://www.calm.com/blog/breathing") == "상업"
    assert rag_compare.academic_ident("https://pubmed.ncbi.nlm.nih.gov/17291166/") == (
        "pmid:17291166"
    )
    assert rag_compare.academic_ident("https://pmc.ncbi.nlm.nih.gov/articles/PMC123/") == (
        "pmcid:PMC123"
    )
    assert rag_compare.academic_ident("https://doi.org/10.1016/j.jenvp.2005.07.001") == (
        "https://doi.org/10.1016/j.jenvp.2005.07.001"
    )
    assert rag_compare.academic_ident("https://blog.naver.com/abc") is None


def test_page_text_prefers_meta_abstract_and_drops_boilerplate():
    raw = ("<html><head><meta name='citation_abstract' content='Abstract here.'></head>"
           "<body><nav>Menu</nav><script>x()</script><p>Body &amp; text</p></body></html>")
    assert rag_compare.page_text(raw) == "Abstract here. Body & text"


def test_price_counts_cached_tokens_and_web_calls():
    usage = {"input": 1_000_000, "cached": 500_000, "output": 0}
    # 미니: 입력 $0.75 · 캐시 $0.075 — 캐시된 50만은 캐시 단가, 검색 1회 $0.01
    assert rag_compare.price_usd("gpt-5.4-mini", usage, web_calls=1) == (
        0.5 * 0.75 + 0.5 * 0.075 + 0.01
    )


def _run(mode, mentions, repeat=0, judgment=None):
    return {
        "case": "S1-pre-a", "repeat": repeat, "error": None, "mentions": mentions,
        "unattributed": [], "cost_usd": 0.01, "seconds": 1.0,
        "tokens": {"input": 10, "output": 1},
        "judgment": judgment or {"hedged": "PASS", "personal": "FAIL"},
    }


def test_summarize_counts_status_support_and_stability():
    found = {"status": "FOUND", "source_type": "학술", "source_text": "x", "record": {"id": "W1"},
             "judge": {"support": "SUPPORTED", "condition": "MATCH"}}
    missing = {"status": "NOT_FOUND", "source_type": None, "source_text": None, "record": None,
               "citation": {"title": "Made up"},
               "judge": {"support": "UNVERIFIABLE", "condition": "UNKNOWN"}}
    payload = {
        "meta": {"mode": "llm", "model": "m", "repeat": 2},
        "runs": [_run("llm", [found, missing]), _run("llm", [dict(found)], repeat=1)],
    }
    s = rag_compare.summarize(payload)
    # 인용 수 · 실존은 1회차만, Judge 지표는 채점한 run 전부(여기서는 두 회차 다 채점했다)
    assert s["runs"] == 1 and s["mentions"] == 2
    assert s["status"] == {"FOUND": 1, "NOT_FOUND": 1}
    assert s["source_types"] == {"학술": 1, "없음": 1}
    assert s["support"] == {"SUPPORTED": 2, "UNVERIFIABLE": 1}
    assert s["support_with_text"] == [2, 2]
    assert s["hedged"] == [2, 2] and s["personal"] == [0, 2]
    # 반복 2회: 1회차 {W1, Made up} · 2회차 {W1} → Jaccard 1/2, 1회차 인용 중 매번 나온 것 1/2
    assert s["stability"] == {"jaccard": 0.5, "kept": [1, 2]}
    table = rag_compare.format_table([s])
    assert "| 인용 실존 — 없음(NOT_FOUND) | 1/2 (50%) |" in table
