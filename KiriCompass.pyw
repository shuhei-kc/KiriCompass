"""KiriCompass 前例ビューアの起動 (Windows ダブルクリック用)。

.pyw は pythonw (コンソール無し) に関連付けられるため、ウィンドウを
一切開かずGUIだけが立ち上がる (python.org版の標準インストールで
関連付けは自動設定される)。関連付けが無い等でうまく動かない場合は
KiriCompass.bat を使う (コンソールが一瞬出るが、エラーが読める)。

pythonw ではstderrが見えないため、起動時の例外はダイアログで表示する。
"""

import runpy
from pathlib import Path


def _run() -> None:
    gui = Path(__file__).resolve().parent / "tools" / "precedent_gui.py"
    runpy.run_path(str(gui), run_name="__main__")


try:
    _run()
except SystemExit:
    raise
except Exception:
    import traceback
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("KiriCompass 起動エラー", traceback.format_exc())
    except Exception:  # noqa: BLE001 - 表示手段が無ければ諦めて再送出
        pass
    raise
