# 使う前に必要ならインストールしてください:
# pip install janome
# （SudachiPyも使う場合）pip install sudachipy sudachidict-core

from typing import List, Iterable, Literal, Optional
import os
import json
import asyncio
import random

class JapaneseNounExtractor:
    """
    日本語の形態素解析を行い、一般名詞 or 固有名詞のみを返すユーティリティ。
    engine='janome'（既定）または 'sudachi' を選べます。

    Parameters
    ----------
    engine : Literal['janome', 'sudachi']
        使用する形態素解析エンジン。既定は 'janome'。
    normalize : bool
        見出し語（基本形）で返すかどうか。既定 True（表記ゆれを抑えたいときに便利）。
    unique : bool
        重複を削除して返すか。既定 False（出現順を保持）。
    """

    def __init__(
        self,
        engine: Literal['janome', 'sudachi'] = 'janome',
        normalize: bool = True,
        unique: bool = False,
    ):
        self.engine = engine
        self.normalize = normalize
        self.unique = unique
        self.count = 0
        self.is_speak_filler = False
        self.list_keyword_templates = self.load_keyword_templates()

        if engine == 'janome':
            from janome.tokenizer import Tokenizer
            udic_path = "./janome_dictionary/user_dictionary.csv"
            self._tokenizer = Tokenizer(udic=udic_path, udic_enc="utf8")
        elif engine == 'sudachi':
            # SudachiPy は辞書が必要です。標準辞書: sudachidict-core
            from sudachipy import dictionary, tokenizer
            self._tokenizer = dictionary.Dictionary().create()
            self._mode = tokenizer.Tokenizer.SplitMode.C  # C=最長単位、A=細かく
        else:
            raise ValueError("engine must be 'janome' or 'sudachi'")

    def load_keyword_templates(self, path: Optional[str] = None):
        try:
            base_dir = os.path.dirname(__file__)
            json_path = path or os.path.join(base_dir, "keyword_filler.json")
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # そのまま辞書形式で保持・返却（結合しない）
            self.list_keyword_templates = data
            return data
        except Exception as e:
            try:
                print(f"keyword_filler.json 読み込みエラー: {e}")
            except Exception:
                pass
            return []

    def extract(self, text: str) -> List[str]:
        """テキストから一般名詞・固有名詞のみを抽出して返します。"""
        if not text:
            return []

        if self.engine == 'janome':
            nouns = self._extract_janome(text)
        else:
            nouns = self._extract_sudachi(text)

        if self.unique:
            seen = set()
            deduped = []
            for n in nouns:
                if n not in seen:
                    seen.add(n)
                    deduped.append(n)
            return deduped
        return nouns

    # 関数呼び出し風に使えるように
    __call__ = extract

    # --- 内部実装: Janome ---
    def _extract_janome(self, text: str) -> List[str]:
        """
        Janome の品詞体系（例）:
          名詞,一般 / 名詞,固有名詞,人名,... / 名詞,サ変接続 など
        """
        results: List[str] = []
        for t in self._tokenizer.tokenize(text):
            pos = t.part_of_speech.split(',')  # e.g. ['名詞', '固有名詞', '一般', '*']
            if pos[0] != '名詞':
                continue
            # 一般名詞または固有名詞だけ採用
            is_common = len(pos) > 1 and pos[1] == '一般'
            is_proper = len(pos) > 1 and pos[1] == '固有名詞'
            if not (is_common or is_proper):
                continue

            surface = t.base_form if self.normalize and t.base_form != '*' else t.surface
            # 空文字/記号除去の簡易チェック
            if surface and surface.isascii() is False or surface:
                results.append(surface)
        return results

    # --- 内部実装: SudachiPy ---
    def _extract_sudachi(self, text: str) -> List[str]:
        """
        Sudachi の品詞体系（例）:
          ['名詞','普通名詞','一般',...], ['名詞','固有名詞','人名',...]
        '普通名詞' or '固有名詞' を対象にします。
        """
        results: List[str] = []
        for m in self._tokenizer.tokenize(text, self._mode):
            pos = m.part_of_speech()  # tuple
            if len(pos) < 2 or pos[0] != '名詞':
                continue
            is_common = pos[1] in ('普通名詞', '一般')  # dictにより末端の語が'一般'になる場合がある
            is_proper = pos[1] == '固有名詞'
            if not (is_common or is_proper):
                continue

            surface = m.normalized_form() if self.normalize else m.surface()
            if surface:
                results.append(surface)
        return results
    
    def pos_all(self, text: str) -> List[dict]:
        """
        与えられた文章を形態素解析し、各トークンの品詞情報を配列で返します。
        戻り値の各要素は辞書で、少なくとも以下のキーを含みます。
          - surface: 表層形
          - base: 見出し語/基本形（取得不可の場合は表層形）
          - pos: 品詞情報（配列）
        可能なら読みや正規化形なども含めます。
        """
        if not text:
            return []
        if self.engine == 'janome':
            return self._pos_all_janome(text)
        return self._pos_all_sudachi(text)

    # --- 内部実装: 品詞一覧出力 (Janome) ---
    def _pos_all_janome(self, text: str) -> List[dict]:
        list_tokens: List[dict] = []
        for t in self._tokenizer.tokenize(text):
            pos_list = t.part_of_speech.split(',') if t.part_of_speech else []
            base = t.base_form if getattr(t, 'base_form', '*') != '*' else t.surface
            list_tokens.append({
                'surface': t.surface,
                'base': base,
                'pos': pos_list,
                'reading': getattr(t, 'reading', None),
                'phonetic': getattr(t, 'phonetic', None),
            })
        return list_tokens

    # --- 内部実装: 品詞一覧出力 (Sudachi) ---
    def _pos_all_sudachi(self, text: str) -> List[dict]:
        list_tokens: List[dict] = []
        for m in self._tokenizer.tokenize(text, self._mode):
            pos_tuple = m.part_of_speech() or ()
            list_tokens.append({
                'surface': m.surface(),
                'base': m.dictionary_form(),
                'normalized': m.normalized_form(),
                'pos': list(pos_tuple),
                'reading': m.reading_form(),
            })
        return list_tokens
        
    async def make_keyword_filler_async(self, text: str, your_name: str) -> tuple[str, str]:
        nouns = await asyncio.to_thread(self.extract, text)
        # your_name を除外
        try:
            listNouns = [n for n in nouns if your_name not in n]
        except Exception:
            listNouns = nouns
        print(listNouns)
        if listNouns != [] and not self.is_speak_filler:
            self.count += 1
            if self.count >= 2:
                self.is_speak_filler = True
                # 辞書形式から個別リストを参照（結合しない）
                obj = self.list_keyword_templates or {}
                kw = obj.get("keyword", {}) if isinstance(obj, dict) else {}
                list_main = kw.get("keyword_filler", [])
                list_add = kw.get("keyword_filler_add", [])

                return_message = random.choice(list_main).format(keyword=listNouns[0], your_name=your_name)
                #return_message += random.choice(list_add).format(keyword=nouns[0])
                return return_message, listNouns[0]
        return ""
    def reset_keyword_filler(self) -> None:
        self.count = 0
        self.is_speak_filler = False
        return

    


# ========== 使用例 ==========
if __name__ == "__main__":
    text = "昨日、東京スカイツリーでイベントがあり、任天堂の新作ゲームが話題になりました。"

    # Janome（既定）
    extractor = JapaneseNounExtractor(engine="janome", normalize=True, unique=True)
    print(extractor.extract(text))
    # 例）['昨日', '東京', 'スカイツリー', 'イベント', '任天堂', '新作', 'ゲーム', '話題']

    text = "好きな人は誰"
    print(extractor.pos_all(text))

    # SudachiPy を使う場合
    # extractor2 = JapaneseNounExtractor(engine="sudachi", normalize=True, unique=False)
    # print(extractor2(text))
