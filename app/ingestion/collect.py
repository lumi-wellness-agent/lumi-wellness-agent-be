"""논문 수집 — OpenAlex에서 서지·OA 링크를, Europe PMC에서 PMCID와 전문 XML을 가져온다.

새 문헌 검색은 월 1회를 가정한다(기획서 7). 반영 전에 적합성·중복·라이선스를 검수한다.

- OpenAlex: `GET https://api.openalex.org/works/doi:<DOI>`
  - 초록은 평문이 아니라 `abstract_inverted_index`(단어 → 위치 목록)로 온다.
    위치 순서대로 다시 이어 붙여야 한다.
  - OA 원문 PDF는 `best_oa_location.pdf_url`, PMID는 `ids.pmid`에 있다.
  - 키 없이 호출되지만 하루 사용량 한도가 있고, 남은 양은 응답 헤더 `x-ratelimit-*`로 온다.
    키 없이 1000크레딧이고, DOI 단건 조회는 0크레딧, 검색(`/works?search=`)은 10크레딧이었다.
    무료 키는 한도가 10배다. `api_key` 쿼리 파라미터나 `Authorization: Bearer`로 보낸다.
- Europe PMC: `GET https://www.ebi.ac.uk/europepmc/webservices/rest/search`
  `?query=DOI:"<DOI>"&format=json&resultType=core`
  - `resultList.result[]`에 `pmcid`, `isOpenAccess`("Y"/"N"), `abstractText`,
    `fullTextUrlList`가 있다. PubMed 레코드는 `source`가 `MED`이고 `pmid`가 있다.
  - PMC에 전문이 있으면 `GET .../rest/<PMCID>/fullTextXML`이 JATS XML을 준다(parse.py).
"""
