#!/usr/bin/env python3
"""Measure document-level expression coverage in the local OCR corpus."""
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

ROOT = Path("output/markdown")
OUT = Path("output/analysis")


def normalize(text):
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"<[^>]*>", "", text)
    return re.sub(r"\s+", "", text.replace("\\", "").replace("#", ""))


def load_exam(folder):
    manifest = json.loads((folder / "ocr-manifest.json").read_text())
    first = 1 if folder.name == "2024-12-N2" else 3
    records = []
    phase = "vocabulary"
    listening_problem = None
    actual_listening_item = False
    for page in range(first, manifest["pages"] + 1):
        correction = folder / "corrections" / f"page-{page:02d}.md"
        source = correction if correction.exists() else folder / "ocr" / f"page-{page:02d}.txt"
        for line_number, raw in enumerate(source.read_text().splitlines(), 1):
            text = normalize(raw)
            if not text:
                continue
            if re.match(r"^(参考答案|参考答|正答表|解答一覧)", text):
                phase = "answers"
            heading = re.match(r"^(?:聴解)?[問間岡同]題(\d{1,2})", text)
            number = int(heading[1]) if heading else None
            if phase == "vocabulary" and number == 7:
                phase = "grammar"
            elif phase == "grammar" and number is not None and number >= 10:
                phase = "reading"
            elif phase == "reading" and number == 1:
                phase = "listening"
            if phase == "listening" and heading:
                listening_problem = number
                actual_listening_item = False
            if phase == "listening" and re.match(r"^[1-9][.、]?番", text):
                actual_listening_item = True
            # Exclude repeated directions, page numbers, and covers.
            if heading or re.fullmatch(r"[-ー一]*\d+[-ー一]*", text):
                continue
            if re.search(r"選びなさい|選んでください|印刷されて|いんさつされ|問題用紙|読む時間が|試験時間", text):
                continue
            records.append({"page": page, "line": line_number,
                            "phase": phase, "text": text, "raw": raw,
                            "listening_actual": phase == "listening" and actual_listening_item
                            and listening_problem in (1, 2, 5)})
    return {"id": folder.name, "records": records}


GRAMMAR = {
    "はず": r"はず",
    "わけではない": r"わけ(?:では|じゃ|でも)な",
    "わけにはいかない": r"わけに(?:は|も)?(?:いか|行か)",
    "わけだ": r"わけ(?:だ|です|で(?![はもな]))",
    "に違いない": r"に(?:違い|ちがい)な",
    "とは限らない": r"とは(?:限ら|かぎら)な",
    "に決まっている": r"に(?:決まっ|きまっ)て",
    "かもしれない": r"かもしれ",
    "らしい": r"らしい|らしく",
    "としても": r"としても",
    "にしても": r"にしても",
    "にしては": r"にしては",
    "といっても": r"と(?:い|言)っても",
    "からといって": r"からと(?:い|言)って",
    "からには": r"からには",
    "からこそ": r"からこそ",
    "以上は": r"以上は",
    "ものの": r"ものの",
    "ものなら": r"ものなら",
    "ものだから": r"ものだから",
    "ものだ": r"もの(?:だ|で|です|では)",
    "ことにする": r"ことに(?:し|す)",
    "ことになる": r"ことに(?:な|なる)",
    "ことはない": r"ことはな",
    "ことなく": r"ことなく",
    "ということ": r"ということ",
    "ことから": r"ことから",
    "ばかりか": r"ばかりか",
    "ばかりに": r"ばかりに",
    "どころか": r"どころか",
    "どころではない": r"どころではな",
    "だけあって": r"だけあって",
    "だけに": r"だけに",
    "にすぎない": r"に(?:すぎ|過ぎ)な",
    "にほかならない": r"に(?:ほか|他)ならな",
    "にもかかわらず": r"にも(?:かかわ|関わ)らず",
    "を問わず": r"を(?:問わ|とわ)ず",
    "にわたって": r"にわた(?:っ|り|る)",
    "に際して": r"に(?:際し|さいし)",
    "にあたって": r"に(?:あたっ|当たっ)",
    "に先立って": r"に(?:先立|さきだ)",
    "うちに": r"うちに",
    "最中": r"最中|さいちゅう",
    "たびに": r"(?:たび|度)に",
    "ついでに": r"ついでに",
    "次第": r"次第|しだい",
    "に伴って": r"に(?:伴|ともな)",
    "につれて": r"につれ",
    "にしたがって": r"に(?:したが|従)っ",
    "一方": r"一方",
    "反面": r"反面",
    "に応じて": r"に(?:応じ|おうじ)",
    "に対して": r"に(?:対し|たいし)",
    "にとって": r"にとって",
    "によって": r"によ(?:っ|る|り)",
    "について": r"について",
    "に関して": r"に(?:関し|かんし)",
    "において": r"にお(?:い|ける)",
    "を通して": r"を(?:通し|とおし)",
    "を通じて": r"を(?:通じ|つうじ)",
    "をめぐって": r"をめぐ",
    "をもとに": r"を(?:もと|基)に",
    "せい": r"せい(?:で|か|だ|に)",
    "おかげ": r"おかげ",
    "あまり": r"あまり",
    "ないことはない": r"ないこと(?:は|も)な",
    "ずにはいられない": r"ずにはいられ",
    "ざるを得ない": r"ざるを(?:得|え)",
    "かねない": r"かねな",
    "かねる": r"かね(?:る|ま|た|て)",
    "っこない": r"っこな",
    "ようがない": r"ようがな",
    "べき": r"べき",
    "なければならない": r"なければ(?:なら|いけ)|なくては(?:なら|いけ)",
    "なくてもいい": r"なくても(?:い|よ)",
    "てからでないと": r"てからでな(?:いと|ければ)",
    "ところだった": r"ところだっ",
    "さえ": r"さえ",
    "こそ": r"こそ",
    "ほど": r"ほど",
    "いただく": r"いただ(?:く|き|け|い|か|こ)",
    "くださる": r"くださ",
    "もらう": r"もら",
    "さしあげる": r"さしあげ|差し上げ",
    "おっしゃる": r"おっしゃ",
    "いらっしゃる": r"いらっしゃ",
    "伺う": r"伺|うかが",
    "ご覧になる": r"ご覧",
    "ておく": r"てお(?:く|き|い|け)|でお(?:く|き|い|け)",
    "てしまう": r"てしま|でしま",
    "てみる": r"てみ(?:る|た|て|ま|な|れ)",
    "ていく": r"てい(?:く|き|っ|け)",
    "てくる": r"て(?:くる|きた|きて|きま|こな)",
    "ようになる": r"ようにな",
    "ようにする": r"ように(?:す|し)",
}

GROUPS = {
    "判断・推量・否定": ["はず", "わけではない", "わけにはいかない", "わけだ", "に違いない", "とは限らない", "に決まっている", "かもしれない"],
    "条件・让步・转折": ["としても", "にしても", "にしては", "といっても", "からといって", "からには", "以上は", "ものの", "ものなら", "にもかかわらず"],
    "形式名词与句尾": ["ものだ", "ものだから", "ことにする", "ことになる", "ことはない", "ということ", "わけだ", "ところだった"],
    "授受・敬语": ["いただく", "くださる", "もらう", "さしあげる", "おっしゃる", "いらっしゃる", "伺う", "ご覧になる"],
    "复合助词": ["に応じて", "に対して", "にとって", "によって", "について", "に関して", "において", "を通して", "を通じて", "をめぐって", "をもとに", "を問わず"],
    "时间・阶段・顺序": ["うちに", "最中", "たびに", "ついでに", "次第", "に際して", "にあたって", "に先立って", "てからでないと", "ところだった"],
    "动作状态与变化": ["ておく", "てしまう", "てみる", "ていく", "てくる", "ようになる", "ようにする"],
    "限定・程度・追加": ["ばかりか", "ばかりに", "どころか", "だけあって", "だけに", "にすぎない", "さえ", "こそ", "ほど"],
}

VOCABULARY = """
確認 変更 連絡 相談 説明 案内 申込 申し込 予約 予定 都合 準備 手続 提出 参加
中止 延期 取消 取り消 追加 削除 修正 対応 調整 注文 注目 判断 断る 断っ 任せ 任さ
任務 役目 責任 担当 係 関係 影響 状況 状態 内容 方法 条件 原因 理由 結果 目的
効果 評価 評判 判断 意見 主張 印象 活躍 活動 作業 実施 実行 実現 実践 達成
能力 態度 表情 様子 格好 外見 容姿 性格 人柄 充実 鮮明 明確 具体 積極 消極
順調 慎重 適切 適当 確実 正確 確か 無事 有効 豊富 優秀 貴重 余裕 納得 承認
指示 指導 募集 採用 就職 勤務 出勤 欠席 出席 営業 残業 費用 料金 代金 値段
費用 支払 払う 収入 利益 予算 節約 減少 増加 減ら 増や
いきなり うっかり すっかり しっかり はっきり さっぱり たっぷり がっかり
ぎっしり そっくり ぴったり すっきり ぐっすり のんびり ぎりぎり どきどき
ぐずぐず ばらばら ふらふら ぶらぶら ぼろぼろ ぐるぐる いらいら わくわく
かさかさ べたべた どろどろ ふさふさ こつこつ さっさと きっぱり じっくり
まもなく そろそろ いよいよ とうとう ついに つい つねに 常に たまたま
とりあえず あいにく せっかく わざわざ せめて むしろ かえって さすが
めったに 必ずしも 必ず まさか どうも どうせ どうにか いつのまにか
徐々に 次第に 次々に 早速 直ちに 一応 事前 案の定 相変わらず お互い
サービス スケジュール メニュー プラン スタッフ アルバイト キャンセル
アンケート アドバイス トラブル サンプル コミュニケーション チャンス
イメージ ストレス ニーズ リハーサル プレッシャー リラックス リズム
マイペース スムーズ スピード バランス ポイント チャレンジ アイデア
講義 講演 授業 研究 資料 報告 発表 実験 調査 研修 会議 打ち合わせ
運賃 管理 資源 維持 利用 修理 解決 改善 選択 比較 情報 提供
取り扱 とりあつか 取り組 とりく 取り消 とりけ 取り替 とりか 取り戻 とりもど
引き受 ひきうけ 引き継 ひきつ 引き返 ひきかえ 引っ越 ひっこし
見直 みなお 見込 みこ 見送 みおく 申し出 もうしで 思い込 おもいこ
思い出 おもいだ 思い切 おもいき 気づ 気に入 気にな 気をつけ
""".split()

VOCAB_GROUPS = {
    "副词・拟态词": VOCABULARY[VOCABULARY.index("いきなり"):VOCABULARY.index("お互い")],
    "片假名词": [w for w in VOCABULARY if re.fullmatch("[ァ-ヶー]+", w)],
    "事务办理": "確認 変更 連絡 相談 説明 案内 申込 申し込 予約 予定 都合 準備 手続 提出 参加 中止 延期 取消 取り消 追加 削除 修正 対応 調整".split(),
    "状态・评价・抽象词": "影響 状況 状態 内容 方法 条件 原因 理由 結果 目的 効果 評価 評判 判断 意見 主張 印象 態度 表情 様子 格好 外見 容姿 性格 人柄 充実 鮮明 明確 具体 積極 消極 順調 慎重 適切 適当 確実 正確 確か 無事 有効 豊富 優秀 貴重 余裕 納得".split(),
}


def count_patterns(exams, phase, patterns):
    result = {}
    for label, pattern in patterns.items():
        expression = re.compile(pattern)
        occurrences = []
        for exam in exams:
            hits = [dict(record, match=expression.search(record["text"])[0])
                    for record in exam["records"]
                    if record["phase"] == phase and expression.search(record["text"])
                    and (phase != "listening" or record["listening_actual"])]
            if hits:
                occurrences.append({"exam": exam["id"], "hits": hits})
        result[label] = {"document_count": len(occurrences), "occurrences": occurrences}
    return dict(sorted(result.items(), key=lambda item: (-item[1]["document_count"], item[0])))


def count_groups(counts, definitions):
    output = {}
    for label, members in definitions.items():
        exams = set()
        for member in members:
            exams.update(e["exam"] for e in counts.get(member, {}).get("occurrences", []))
        output[label] = {"document_count": len(exams), "members": members, "exams": sorted(exams)}
    return output


def main():
    exams = [load_exam(folder) for folder in sorted(ROOT.glob("*-N2"))]
    grammar = count_patterns(exams, "grammar", GRAMMAR)
    words = [w for w in VOCABULARY if w not in ("つい", "係", "必ず")]
    vocab = count_patterns(exams, "vocabulary", {w: re.escape(w) for w in words})
    listening = count_patterns(exams, "listening", {w: re.escape(w) for w in words})
    katakana = count_patterns(exams, "vocabulary", {"片假名词": r"[ァ-ヴー]{3,}"})
    result = {
        "documents": len(exams),
        "method": "Document-level literal regex coverage within sections; includes stems and distractors; not correct-answer counts or future probabilities.",
        "sections": {
            exam["id"]: {
                phase: {"characters": sum(len(r["text"]) for r in exam["records"] if r["phase"] == phase),
                        "pages": sorted({r["page"] for r in exam["records"] if r["phase"] == phase})}
                for phase in ("vocabulary", "grammar", "reading", "listening", "answers")}
            for exam in exams
        },
        "grammar": grammar, "grammar_groups": count_groups(grammar, GROUPS),
        "vocabulary": vocab, "vocabulary_groups": count_groups(vocab, VOCAB_GROUPS),
        "listening_printed_options_excluding_examples": listening,
    }
    result["vocabulary_groups"]["片假名词"] = katakana["片假名词"]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "exam-frequency.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    (OUT / "sections.json").write_text(json.dumps(exams, ensure_ascii=False, indent=2) + "\n")
    for label, values in (("GRAMMAR GROUPS", result["grammar_groups"]),
                          ("GRAMMAR", grammar), ("VOCABULARY GROUPS", result["vocabulary_groups"]),
                          ("VOCABULARY", vocab), ("LISTENING PRINTED OPTIONS", listening)):
        print(label)
        print([(k, v["document_count"]) for k, v in list(values.items())[:45]])
    print("SECTION SIZES", [(exam, {k: v["characters"] for k, v in sections.items()})
                            for exam, sections in result["sections"].items()])


if __name__ == "__main__":
    main()
