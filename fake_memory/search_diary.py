import glob
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import random

def search_in_file(filepath: str, keyword: str) -> str | None:
    """ファイル内にキーワードが含まれていたら中身を返す"""
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            if keyword in content:
                return content
    except Exception as e:
        print(f"Error reading {filepath}: {e}")
    return None

def search_diaries(keyword: str, max_workers: int = 8) -> dict:
    """
    フォルダ内の *_diary.txt を対象にキーワード検索。
    ヒットした {ファイル名: 内容} を返す。
    """
    folder = "./fake_memory/diary"
    results = {}
    files = glob.glob(os.path.join(folder, "*_diary.txt"))
    random.shuffle(files)
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_file = {executor.submit(search_in_file, f, keyword): f for f in files}
        
        for future in as_completed(future_to_file):
            file = future_to_file[future]
            content = future.result()
            if content:
                results[file] = content
    return results


# 使い方例
if __name__ == "__main__":
    keyword = "旅行"
    matches = search_diaries(keyword, max_workers=16)

    for f, text in matches.items():
        print(f"\n=== {f} ===\n")
        print(text[:500], "...")  # 長い場合は冒頭500文字だけ表示
