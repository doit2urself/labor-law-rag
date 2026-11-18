// 고정 안내 문구 묶음.
// 사용처: App.tsx(화면 맨 위: 자문 아님 + 개인정보 주의), AnswerView.tsx(답변 아래: 법제처 출처 표기).
//
// 왜 컴포넌트로 모았나: 이 문구들은 법적·서비스 성격상 빠지면 안 되고(출처 미표기는 법제처 이용안내상 처벌 대상이 될 수 있다),
// 글자가 한 곳에서만 정해져야 어긋나지 않는다. 문구 자체는 texts.ts에 있고 백엔드(app/texts.py)와 같은지 파이썬 테스트가 확인한다.
import { DISCLAIMER, PII_NOTICE, SOURCE_NOTICE } from "../texts";

interface NoticesProps {
  /** top: 자문 아님·개인정보 주의 / bottom: 법제처 출처 표기 / all(기본): 셋 모두 */
  place?: "top" | "bottom" | "all";
}

export function Notices({ place = "all" }: NoticesProps) {
  const showTop = place !== "bottom";
  const showBottom = place !== "top";
  return (
    <div className={`notices notices-${place}`}>
      {showTop && (
        <>
          <p className="notice notice-disclaimer">{DISCLAIMER}</p>
          <p className="notice notice-pii">{PII_NOTICE}</p>
        </>
      )}
      {showBottom && <p className="notice notice-source">{SOURCE_NOTICE}</p>}
    </div>
  );
}
