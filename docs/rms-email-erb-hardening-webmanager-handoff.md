# WebManager JS 렌더러 ERB 하드닝 — 적용 핸드오프

> 대상: **WebManager 세션**에서 직접 적용. 이 문서는 RMS 레포에 있지만 변경 대상은
> `WebManager/client/src/features/rms-email-template/utils/bodyRenderer.js`(+ 테스트)이다.
> RMS 세션(Claude)은 사용자 지시에 따라 WebManager 파일을 일절 수정하지 않았다.

## 배경

2026-06 완결성 감사가 캐논 렌더러(RMS `src/alert/body_renderer.py`)에서 ERB 결함 2건을
찾았고, **Python 측은 이미 수정·검증 완료**(전체 `798 passed`)다. JS 렌더러
`bodyRenderer.js`는 Python canon의 **byte-동일 포트**여야 하므로 같은 수정을 미러해야 한다.

| 이슈 | 증상 | Python 수정 |
|---|---|---|
| #1 (med) | 불균형 ERB(START만 / END 누락 / 역순) → `split` 무방비로 예외. JS는 `indexOf===-1`에서 `slice(0,-1)`로 **깨진 출력** 생성(이미 Python과 발산, golden이 happy-path만이라 미검출) | `_expand_erb`를 *total*로 — 예외 없이 마커 strip |
| #2 (low) | 다중 ERB 블록 → 첫 블록만 전개, 두 번째 마커 리터럴 누출 + 빈 `<tr></tr>` | 첫 균형 블록만 전개, 나머지 블록은 **통째 제거** |

> **서버 측은 조치 불필요**: `server/features/rms-email-template/erbValidation.js` +
> `service.js`가 이미 저장 시 ERB 균형을 검증한다(감사의 "서버 미검증" 주장은 오탐).
> 클라 편집기 lint(`validateTemplate.js`)도 그대로 1차 가드. 이 렌더러 하드닝은 RMS가
> Mongo에서 템플릿을 **직접 읽기** 때문에 seed/마이그레이션/수동 DB 편집이 두 가드를
> 우회했을 때를 위한 **defense-in-depth**다.

## 설계 (Python과 동일하게)

- 렌더러는 **total**: 어떤 입력에도 예외를 던지지 않는다. (JS 프리뷰는 `renderBody`를
  try/except 없이 직접 호출하므로 total이어야 프리뷰가 안 깨진다 — 이게 핵심.)
- 해피패스(단일 균형 블록, 잡 마커 없음) **출력 불변** → golden byte-패리티 유지.
- 불균형(균형 쌍 없음): 마커 strip, 행 미전개.
- 다중/잡 블록: 첫 균형 블록만 전개, 나머지 완전 블록은 통째 제거, 잔여 lone 마커 제거.

## 적용 1 — `bodyRenderer.js`

현재 파일은 REF-2 리팩터로 `ERB_START`/`ERB_END`를 `tokens.js`에서 import한다
(`import { TOKEN_CONTEXT, makeTokenRe, ERB_START, ERB_END, RESERVED_AKKA_TOKEN } from './tokens.js'`).
**새 import 불필요.**

### (a) 헬퍼 추가 — `const SEV_RANK = ...` 줄 *위*에 삽입

```js
// A complete (balanced) ERB block — drop duplicate/leftover blocks whole
// (markers + inner) so a bypass-written template can't leak markers or blank
// rows. Mirrors body_renderer._ERB_BLOCK_RE.
const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const ERB_BLOCK_RE = new RegExp(escapeRe(ERB_START) + '[\\s\\S]*?' + escapeRe(ERB_END), 'g')

function stripErb(text) {
  return text.replace(ERB_BLOCK_RE, '').split(ERB_START).join('').split(ERB_END).join('')
}
```

### (b) `expandErb` 전체 교체

기존:

```js
function expandErb(template, rows, rowLimit, overflowText) {
  const startIdx = template.indexOf(ERB_START)
  if (startIdx === -1) return template
  const pre = template.slice(0, startIdx)
  const rest = template.slice(startIdx + ERB_START.length)
  const endIdx = rest.indexOf(ERB_END)
  const inner = rest.slice(0, endIdx)
  const post = rest.slice(endIdx + ERB_END.length)
  const capped = rowLimit == null ? rows : rows.slice(0, rowLimit)
  const remaining = rows.length - capped.length
  const pieces = capped.map((row, i) =>
    substitute(inner, { ...row, '@Row.Index': i + 1 }, true)
  )
  const overflow = remaining ? overflowText.split('@RemainingCount').join(String(remaining)) : ''
  return pre + pieces.join('') + overflow + post
}
```

신규 (Python `_expand_erb`와 1:1):

```js
function expandErb(template, rows, rowLimit, overflowText) {
  // Total on malformed ERB fences (mirror of body_renderer._expand_erb): the
  // WebManager save-time lint + server validation are the primary guard, but RMS
  // reads templates straight from Mongo so a seed/migration/manual edit can
  // bypass them. Never leak a marker or blank row; only the first balanced block
  // expands and any extra/duplicate block is dropped whole.
  const start = template.indexOf(ERB_START)
  const end = start === -1 ? -1 : template.indexOf(ERB_END, start + ERB_START.length)
  if (start === -1 || end === -1) {
    if (template.includes(ERB_START) || template.includes(ERB_END)) return stripErb(template)
    return template
  }
  let pre = template.slice(0, start)
  const inner = template.slice(start + ERB_START.length, end)
  let post = template.slice(end + ERB_END.length)
  const capped = rowLimit == null ? rows : rows.slice(0, rowLimit)
  const remaining = rows.length - capped.length
  const pieces = capped.map((row, i) =>
    substitute(inner, { ...row, '@Row.Index': i + 1 }, true)
  )
  const overflow = remaining ? overflowText.split('@RemainingCount').join(String(remaining)) : ''
  if (pre.includes(ERB_START) || pre.includes(ERB_END) || post.includes(ERB_START) || post.includes(ERB_END)) {
    pre = stripErb(pre)
    post = stripErb(post)
  }
  let result = pre + pieces.join('') + overflow + post
  if (result.includes(ERB_START) || result.includes(ERB_END)) {  // lone markers from a nested fence
    result = result.split(ERB_START).join('').split(ERB_END).join('')
  }
  return result
}
```

> 알고리즘 대조: `indexOf(ERB_END, start+len)` ↔ Python `find(ERB_END, start+len)`;
> `ERB_BLOCK_RE`(`g`, non-greedy `[\s\S]*?`) ↔ Python `re.sub(START[\s\S]*?END)`;
> `split().join('')` ↔ Python `str.replace(...,'')`. 로깅은 출력에 무관하므로 JS는 생략.

## 적용 2 — 회귀 테스트 (`tests/bodyRenderer.test.js`)

Python `TestMalformedErbIsTotal` 4건을 미러. **아래 expected는 Python canon이 실제로
산출한 정확한 문자열**(byte-패리티 오라클)이다:

```js
import { describe, it, expect } from 'vitest'
import { renderBody } from '../utils/bodyRenderer.js'

describe('malformed ERB is total (mirrors body_renderer TestMalformedErbIsTotal)', () => {
  it('start without end → no throw, marker stripped', () => {
    const out = renderBody('<table><!--@EachEquipment--><tr><td>@Row.EqpId</td></tr></table>', {}, [{ '@Row.EqpId': 'EQP001' }])
    expect(out).toBe('<table><tr><td></td></tr></table>')
  })
  it('end only → marker stripped', () => {
    const out = renderBody('<p>x</p><!--@EndEachEquipment-->', {}, [])
    expect(out).toBe('<p>x</p>')
  })
  it('leading stray end + valid block → first block still expands', () => {
    const out = renderBody('<!--@EndEachEquipment--><table><!--@EachEquipment--><tr>@Row.EqpId</tr><!--@EndEachEquipment--></table>', {}, [{ '@Row.EqpId': 'EQP001' }])
    expect(out).toBe('<table><tr>EQP001</tr></table>')
  })
  it('duplicate blocks → second dropped whole (no marker, no blank row)', () => {
    const tpl =
      '<table><!--@EachEquipment--><tr>R1=@Row.EqpId</tr><!--@EndEachEquipment-->' +
      'MID<!--@EachEquipment--><tr>R2=@Row.EqpId</tr><!--@EndEachEquipment--></table>'
    const out = renderBody(tpl, {}, [{ '@Row.EqpId': 'EQP001' }])
    expect(out).toBe('<table><tr>R1=EQP001</tr>MID</table>')
  })
})
```

## 검증

```bash
cd WebManager/client
npm test -- src/features/rms-email-template      # 새 4건 통과
# 특히 golden 드리프트 가드(golden_copy_in_sync) + bodyRenderer golden 케이스가
# 여전히 그린이어야 함 → 해피패스 출력 불변(byte-패리티) 확인
npm run build                                     # 빌드 성공
```

수용 기준:
- 새 malformed 4건 통과(위 expected와 정확히 일치).
- 기존 golden/bodyRenderer 케이스 전부 그린(출력 불변).
- 기존 무관 실패 13건(`features/clients/components/config-form` ResourceAgent)은 그대로 무시.

## 참고 — Python canon 변경(이미 적용/검증됨, RMS 레포)

- `src/alert/body_renderer.py`: `_ERB_BLOCK_RE` 추가, `_strip_erb` 추가, `_expand_erb` total화.
- `tests/unit/test_body_renderer.py`: `TestMalformedErbIsTotal` 4건.
- `tests/unit/test_alert_builder.py`: 불균형 ERB가 더 이상 폴백을 유발하지 않음을 반영해
  `test_render_error_falls_back_to_default`를 (best-effort 렌더 1건 + monkeypatch 안전망 1건)으로 분리.
- 결과: `798 passed, 1 skipped`.
