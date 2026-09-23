import os
import re
import time
from dataclasses import dataclass
from typing import Any

import requests


def _text(value: Any, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _timestamp_seconds(value: str) -> float:
    parts = [part.strip() for part in str(value or "").split(":")]
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return 0.0
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return numbers[0] if numbers else 0.0


def _time_range(value: str) -> tuple[float, float]:
    raw = str(value or "")
    start_raw, separator, end_raw = raw.partition("-")
    if not separator:
        return 0.0, 0.0
    return max(0.0, _timestamp_seconds(start_raw)), max(0.0, _timestamp_seconds(end_raw))


@dataclass(frozen=True)
class RedlineRule:
    code: str
    category: str
    severity: str
    title: str
    pattern: re.Pattern[str]


DEFAULT_RULES = (
    RedlineRule(
        "internal-medical-hard",
        "internal",
        "hard",
        "医疗化或绝对功效承诺",
        re.compile(r"根治|治愈|治好|药到病除|100\s*%\s*(?:有效|安全)|百分之百(?:有效|安全)|零过敏|绝不反弹|永久祛", re.I),
    ),
    RedlineRule(
        "platform-diversion-hard",
        "platform",
        "hard",
        "站外导流或私下交易",
        re.compile(r"加\s*(?:微信|微|v信|vx)|扫码.{0,6}(?:加|联系)|微信号|私下交易|站外下单", re.I),
    ),
    RedlineRule(
        "platform-false-identity-hard",
        "platform",
        "hard",
        "虚假医生、专家或官方身份",
        re.compile(r"(?:假冒|冒充).{0,8}(?:医生|医师|专家|官方)|假医生|假专家", re.I),
    ),
    RedlineRule(
        "platform-sensitive-hard",
        "platform",
        "hard",
        "未成年人或低俗敏感内容",
        re.compile(r"未成年人.{0,10}(?:裸露|性暗示)|色情|淫秽|低俗裸露", re.I),
    ),
    RedlineRule(
        "artist-unauthorized-hard",
        "artist",
        "hard",
        "明确出现未经授权的艺人权益",
        re.compile(r"(?:未经授权|无授权|未获授权).{0,12}(?:明星|艺人|名人|肖像|声音|人脸|换脸|仿声)", re.I),
    ),
    RedlineRule(
        "internal-extreme-warning",
        "internal",
        "warning",
        "极限词、医疗暗示或待举证资质",
        re.compile(r"全网第一|行业第一|国家级|世界级|顶级|唯一|最(?:佳|好|强|有效)|无副作用|抗炎|消炎|处方|药用|权威认证|专利|获奖", re.I),
    ),
    RedlineRule(
        "platform-inducement-warning",
        "platform",
        "warning",
        "互动诱导或利益诱导需复核",
        re.compile(r"点赞.{0,8}(?:领取|抽奖)|关注.{0,8}(?:领取|抽奖)|转发.{0,8}(?:领取|抽奖)|私信领取|拉人助力", re.I),
    ),
    RedlineRule(
        "artist-rights-warning",
        "artist",
        "warning",
        "艺人、肖像或声音授权需复核",
        re.compile(r"明星|艺人|名人|代言人|肖像|换脸|仿声|声音克隆|AI数字人|深度伪造", re.I),
    ),
    RedlineRule(
        "artist-copyright-warning",
        "artist",
        "warning",
        "版权、竞品或平台水印需复核",
        re.compile(r"竞品|平台水印|抖音号|小红书号|字体版权|音乐版权|图片版权", re.I),
    ),
    RedlineRule(
        "internal-ai-defect-warning",
        "internal",
        "warning",
        "AI画面瑕疵或文字异常",
        re.compile(r"手指异常|肢体畸形|穿帮|文字乱码|字幕乱码|画面撕裂|人脸变形|产品变形", re.I),
    ),
    RedlineRule(
        "relax-opening-unrelated",
        "relaxation",
        "warning",
        "开篇无关联可参考放宽",
        re.compile(r"解压|吸睛|噱头|搞笑|表情包|漫画|无关(?:联)?开篇|手工(?:制作|研磨)?|中药研磨|美食制作|农事|装修|显微镜|微观画面|火龙果|海鲜", re.I),
    ),
    RedlineRule(
        "relax-time-dose-immediate",
        "relaxation",
        "warning",
        "具体时间或用量搭配即时功效可参考放宽",
        re.compile(
            r"(?:一抹|一片|一喷|一次|每天|早晚|立即|马上|秒)[^。！？\n]{0,28}(?:补水|保湿|滋润|水润|清洁|净肤|提亮|光泽)"
            r"|(?:补水|保湿|滋润|水润|清洁|净肤|提亮|光泽)[^。！？\n]{0,28}(?:一抹|一片|一喷|一次|每天|早晚|立即|马上|秒)",
            re.I,
        ),
    ),
    RedlineRule(
        "relax-third-party-objective",
        "relaxation",
        "warning",
        "功能升级、多效合一或便捷性对比可参考放宽",
        re.compile(r"功能升级|多效合一|多功效合一|(?:二|三|四|五)效合一|一瓶\s*[=＝]\s*|代替.{0,16}(?:水|乳|精华|面霜|粉底|气垫)|精简.{0,8}(?:护肤|上妆)|在家.{0,10}(?:就能|完成)|不用去.{0,8}(?:美容院|美甲店)", re.I),
    ),
    RedlineRule(
        "relax-comparison-immediate",
        "relaxation",
        "warning",
        "即时功效或真实轻微前后对比可参考放宽",
        re.compile(
            r"(?:补水|保湿|滋润|水润|清洁|净肤|提亮|光泽)[^。！？\n]{0,24}(?:前后对比|对比|使用前|使用后|妆前|妆后)"
            r"|(?:妆前|妆后|使用前|使用后)[^。！？\n]{0,24}(?:补水|保湿|滋润|水润|清洁|净肤|提亮|光泽)",
            re.I,
        ),
    ),
    RedlineRule(
        "relax-marketing-positive",
        "relaxation",
        "warning",
        "正向经历、签约获奖或生日福利事件可参考放宽",
        re.compile(r"回馈粉丝|宠粉|生日.{0,12}(?:福利|活动)|周年庆|签约|获奖|领奖|感谢.{0,12}(?:粉丝|支持|信任)|个人经历|连夜.{0,12}(?:厂家|工厂)|砍价|发福利", re.I),
    ),
    RedlineRule(
        "relax-efficacy-terms",
        "relaxation",
        "warning",
        "抗老、抗衰、逆龄、冻龄、养肤词汇可参考放宽",
        re.compile(r"抗老|抗衰|逆龄|冻龄|养肤", re.I),
    ),
    RedlineRule(
        "relax-saving-language",
        "relaxation",
        "warning",
        "省钱类描述可参考放宽",
        re.compile(r"省下.{0,12}(?:元|块|千|万|LV)|不花冤枉钱|少花冤枉钱|别花冤枉钱|不要.{0,8}花.{0,4}冤枉钱|一瓶顶.{0,8}省|每天.{0,8}不到.{0,8}(?:元|块钱)", re.I),
    ),
    RedlineRule(
        "relax-lab-attire",
        "relaxation",
        "warning",
        "实验室、工厂或单独白大褂元素可参考放宽",
        re.compile(r"白大褂|实验服|手术帽|静电服|防尘服|实验室|工厂车间", re.I),
    ),
    RedlineRule(
        "relax-boundary-unanswered-hook",
        "relaxation",
        "hard",
        "疑问式开篇需核对全片是否解答并关联产品",
        re.compile(r"(?:为什么|原因是什么|怎么办|怎么回事|如何).{0,6}[？?]", re.I),
    ),
    RedlineRule(
        "relax-boundary-time-dose-noninstant",
        "relaxation",
        "hard",
        "具体时间或用量搭配非即时功效仍需重点核对",
        re.compile(
            r"(?:\d+\s*(?:天|周|月|年|小时|分钟|次|片|盒|支|滴)|一天|一周|一个月|一抹|一片|一喷|一次|早晚)[^。！？\n]{0,36}(?:祛痘|祛斑|淡斑|淡纹|抗皱|紧致|抗老|抗衰|逆龄|冻龄|眼袋|法令纹)"
            r"|(?:祛痘|祛斑|淡斑|淡纹|抗皱|紧致|抗老|抗衰|逆龄|冻龄|眼袋|法令纹)[^。！？\n]{0,36}(?:\d+\s*(?:天|周|月|年|小时|分钟|次|片|盒|支|滴)|一天|一周|一个月|一抹|一片|一喷|一次|早晚)",
            re.I,
        ),
    ),
    RedlineRule(
        "relax-boundary-pure-disparagement",
        "relaxation",
        "hard",
        "纯拉踩且未说明自身客观优势仍需重点核对",
        re.compile(r"(?:再贵|普通|别家|其他|那些|市面上).{0,28}(?:都不如|没用|无效|不能|不行|扔掉|别买|不要买|越用越)|(?:都不如|没用|无效|扔掉|别买|不要买).{0,28}(?:普通|别家|其他|市面上)", re.I),
    ),
    RedlineRule(
        "relax-boundary-noninstant-comparison",
        "relaxation",
        "hard",
        "非即时功效前后或多人对比仍需重点核对",
        re.compile(
            r"(?:祛痘|祛斑|淡斑|淡纹|抗皱|紧致|眼袋|法令纹)[^。！？\n]{0,32}(?:前后对比|使用前|使用后|改善前|改善后|多人对比)"
            r"|(?:前后对比|使用前|使用后|改善前|改善后|多人对比)[^。！？\n]{0,32}(?:祛痘|祛斑|淡斑|淡纹|抗皱|紧致|眼袋|法令纹)",
            re.I,
        ),
    ),
    RedlineRule(
        "relax-boundary-negative-event",
        "relaxation",
        "hard",
        "维权、炫富、同行对立或负面冲突事件仍需重点核对",
        re.compile(r"实名举报|一个亿|私人飞机|送金(?:子|条|镯)|网暴|黑粉|同行.{0,8}(?:报复|排挤|抵制)|行业协会|被告了|下跪道歉|维权", re.I),
    ),
    RedlineRule(
        "relax-boundary-medical-setting",
        "relaxation",
        "hard",
        "疑似医护身份与仿医疗环境同时出现仍需重点核对",
        re.compile(
            r"(?:医生|医师|护士|医护|听诊器|手术服)[^。！？\n]{0,50}(?:医院|诊室|手术室|病床|医疗环境)"
            r"|(?:医院|诊室|手术室|病床|医疗环境)[^。！？\n]{0,50}(?:医生|医师|护士|医护|听诊器|手术服)",
            re.I,
        ),
    ),
)

RELAXATION_SOURCE_URL = "https://baizhitian11-alt.github.io/stmc-public-report/audit-relax-0720/#sec-normal"
RELAXED_RULE_CODES = {
    "relax-opening-unrelated",
    "relax-time-dose-immediate",
    "relax-third-party-objective",
    "relax-comparison-immediate",
    "relax-marketing-positive",
    "relax-efficacy-terms",
    "relax-saving-language",
    "relax-lab-attire",
}
RULE_RECOMMENDATIONS = {
    "relax-opening-unrelated": "可参考放宽，但请审核人确认全片至少有 3 秒推广相关画面；疑问式开篇还要确认后文有解答并关联产品。",
    "relax-time-dose-immediate": "补水、保湿、滋润、清洁、提亮等即时功效可参考放宽；最终结合真实可达成程度人工判断。",
    "relax-third-party-objective": "功能升级、多效合一或便捷性客观对比可参考放宽；避免演变为没有事实支撑的纯拉踩。",
    "relax-comparison-immediate": "真实可达成的即时功效和轻微对比可参考放宽；非即时功效对比需按人物与使用过程重点核对。",
    "relax-marketing-positive": "正向感谢、个人经历、签约获奖、生日或粉丝福利可参考放宽；负面冲突、炫富或同行对立不适用。",
    "relax-efficacy-terms": "抗老、抗衰、逆龄、冻龄、养肤词汇本身可参考放宽；其他具体功效、数据和资质仍需单独核对。",
    "relax-saving-language": "省钱类表达可参考放宽；仍请审核人核对是否同时包含其他虚假承诺或恶意拉踩。",
    "relax-lab-attire": "单独实验室、工厂、白大褂或防护服元素可参考放宽；疑似医护身份与仿医疗环境同时出现时需重点核对。",
    "relax-boundary-unanswered-hook": "请审核人检查全片是否回答开篇问题，并明确说明宣传产品与该问题的关系。",
    "relax-boundary-time-dose-noninstant": "祛痘、祛斑、淡纹、抗皱等非即时功效仍需重点核对检测报告、随标和落地页一致性。",
    "relax-boundary-pure-disparagement": "若只否定第三方、未说明自身功能升级、多效合一或便捷性客观事实，建议驳回或修改。",
    "relax-boundary-noninstant-comparison": "请核对是否为同一人非即时功效前后对比，或多人对比但缺少人物 A 使用产品中的演绎画面。",
    "relax-boundary-negative-event": "维权人设、大额炫富、同行对立、权威机构背书或负面冲突营销不属于放宽范围。",
    "relax-boundary-medical-setting": "仅当疑似医护形象与仿医疗环境同时出现才需要重点管控，请结合完整画面人工判断。",
}

# Backwards-compatible alias used by older tests and imports.
RULES = DEFAULT_RULES


def compile_redline_rules(items: list[dict[str, Any]] | None = None) -> tuple[RedlineRule, ...]:
    """Compile persisted rule rows into the same evaluator shape as the defaults."""
    if items is None:
        return DEFAULT_RULES
    compiled: list[RedlineRule] = []
    for item in items:
        if not isinstance(item, dict) or not bool(item.get("enabled", True)):
            continue
        category = _text(item.get("category"), 30)
        severity = _text(item.get("severity"), 20)
        code = _text(item.get("code"), 80)
        title = _text(item.get("title"), 160)
        pattern = str(item.get("pattern") or "").strip()
        if category not in {"platform", "internal", "artist", "relaxation"}:
            raise ValueError(f"规则“{title or code}”的分类无效")
        if severity not in {"hard", "warning"}:
            raise ValueError(f"规则“{title or code}”的处理方式无效")
        if not code or not title or not pattern:
            raise ValueError("AI审核建议规则的编号、名称和命中表达式不能为空")
        try:
            expression = re.compile(pattern, re.I)
        except re.error as error:
            raise ValueError(f"规则“{title}”的命中表达式无效：{error}") from error
        compiled.append(RedlineRule(code, category, severity, title, expression))
    return tuple(compiled)


def normalize_segments(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for index, raw in enumerate(results if isinstance(results, list) else []):
        if not isinstance(raw, dict):
            continue
        start_seconds, end_seconds = _time_range(_text(raw.get("time_range"), 80))
        segment = {
            "index": index + 1,
            "start_seconds": round(start_seconds, 3),
            "end_seconds": round(max(start_seconds, end_seconds), 3),
            "script_text": _text(raw.get("script_text"), 1000),
            "scene_description": _text(raw.get("scene_description"), 1000),
            "scene_text": _text(raw.get("scene_text"), 500),
            "camera_angle": _text(raw.get("camera_angle"), 80),
            "shot_size": _text(raw.get("shot_size"), 80),
            "camera_movement": _text(raw.get("camera_movement"), 80),
        }
        if any(segment[key] for key in ("script_text", "scene_description", "scene_text")):
            segments.append(segment)
    return segments[:80]


def evaluate_redlines(
    results: list[dict[str, Any]],
    configured_rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    segments = normalize_segments(results)
    rules = compile_redline_rules(configured_rules)
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for segment in segments:
        source_parts = [
            ("口播", segment["script_text"]),
            ("画面", segment["scene_description"]),
            ("画面文字", segment["scene_text"]),
        ]
        combined = " ".join(text for _source, text in source_parts if text)
        for rule in rules:
            match = rule.pattern.search(combined)
            key = (rule.code, int(segment["index"]))
            if not match or key in seen:
                continue
            seen.add(key)
            evidence_source, evidence_text = next(
                ((source, text) for source, text in source_parts if rule.pattern.search(text)),
                ("视频理解", combined),
            )
            excerpt_match = rule.pattern.search(evidence_text)
            excerpt = excerpt_match.group(0) if excerpt_match else match.group(0)
            findings.append(
                {
                    "rule_code": rule.code,
                    "category": rule.category,
                    "severity": rule.severity,
                    "title": rule.title,
                    "evidence": _text(excerpt, 160),
                    "evidence_source": evidence_source,
                    "start_seconds": segment["start_seconds"],
                    "end_seconds": segment["end_seconds"],
                    "segment_index": segment["index"],
                    "confidence": "high" if rule.severity == "hard" else "medium",
                    "policy_effect": "relaxed" if rule.code in RELAXED_RULE_CODES else "attention",
                    "recommendation": RULE_RECOMMENDATIONS.get(
                        rule.code,
                        "请审核人结合完整视频、商品事实和授权材料人工判断。",
                    ),
                    "source_url": RELAXATION_SOURCE_URL if rule.code.startswith("relax-") else "",
                }
            )

    hard_count = sum(1 for item in findings if item["severity"] == "hard")
    relaxed_count = sum(1 for item in findings if item["policy_effect"] == "relaxed")
    attention_count = sum(1 for item in findings if item["policy_effect"] == "attention")
    category_counts = {
        category: sum(1 for item in findings if item["category"] == category)
        for category in ("platform", "internal", "artist", "relaxation")
    }
    status = "warning" if findings else "passed"
    if findings:
        summary = (
            f"AI 给出 {attention_count} 条重点关注、{relaxed_count} 条放宽参考；"
            "仅供审核人参考，不自动通过或驳回。"
        )
    else:
        summary = "AI 暂未发现明显风险；仅供审核人参考，最终是否通过仍由审核人决定。"
    return {
        "status": status,
        "summary": summary,
        "findings": findings[:120],
        "segments": segments,
        "category_counts": category_counts,
        "hard_count": hard_count,
        "attention_count": attention_count,
        "relaxed_count": relaxed_count,
    }


class CutterReviewClient:
    def __init__(self) -> None:
        self.base_url = os.getenv(
            "WIS_CUTTER_BASE_URL", "https://cloud.fandow.com/gpt/marketing-video"
        ).strip().rstrip("/")
        self.timeout_seconds = max(60, int(os.getenv("WIS_CUTTER_TIMEOUT_SECONDS", "360")))

    @property
    def configured(self) -> bool:
        return bool(re.match(r"^https?://", self.base_url, re.I))

    def _request(self, path: str, *, method: str = "GET", json: dict | None = None) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("视频理解服务尚未配置")
        response = requests.request(
            method,
            f"{self.base_url}{path}",
            json=json,
            timeout=30,
        )
        try:
            payload = response.json()
        except ValueError as error:
            raise RuntimeError(f"视频理解服务返回了无法解析的结果（HTTP {response.status_code}）") from error
        if not response.ok or str(payload.get("code", "-1")) != "0":
            message = _text(payload.get("message"), 240) or f"HTTP {response.status_code}"
            failure = RuntimeError(f"视频理解请求失败：{message}")
            setattr(failure, "status_code", response.status_code)
            raise failure
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def _select(self, video_url: str) -> list[dict[str, Any]]:
        from urllib.parse import urlencode

        data = self._request(f"/cutter/select?{urlencode({'oss_url': video_url})}")
        results = data.get("results")
        return results if isinstance(results, list) else []

    def analyze(self, video_url: str) -> dict[str, Any]:
        url = _text(video_url, 2048)
        if not re.match(r"^https?://", url, re.I):
            raise RuntimeError("视频理解服务只能读取稳定的 HTTP/HTTPS 视频地址")
        existing = self._select(url)
        if existing:
            return {"task_id": "", "reused": True, "results": existing}

        submitted: dict[str, Any] | None = None
        for attempt in range(4):
            try:
                submitted = self._request("/cutter/save", method="POST", json={"oss_url": url})
                break
            except RuntimeError as error:
                if getattr(error, "status_code", 0) != 429 or attempt == 3:
                    raise
                time.sleep(2 * (2**attempt))
        task_id = _text((submitted or {}).get("task_id"), 160)
        if not task_id:
            raise RuntimeError("视频理解服务没有返回任务 ID")
        if (submitted or {}).get("status") == "success":
            return {"task_id": task_id, "reused": bool((submitted or {}).get("reused")), "results": self._select(url)}

        from urllib.parse import urlencode

        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            time.sleep(2)
            task = self._request(f"/cutter/task?{urlencode({'task_id': task_id})}")
            if task.get("status") == "success":
                results = self._select(url)
                if not results:
                    raise RuntimeError("视频理解任务已完成，但没有返回拆解结果")
                return {"task_id": task_id, "reused": bool(task.get("duplicated")), "results": results}
            if task.get("status") == "failed":
                message = _text(task.get("error_message") or task.get("message"), 240)
                raise RuntimeError(message or "视频理解失败")
        raise RuntimeError("视频理解超时，请稍后重试")


review_ai_service = CutterReviewClient()
