"""错误类型：所有面向用户的解析/溯源错误均使用 LineageError。"""


class LineageError(Exception):
    """携带词法位置信息的溯源错误。"""

    def __init__(self, message, line=None, col=None):
        self.message = message
        self.line = line
        self.col = col
        if line is not None and col is not None:
            full = f"第 {line} 行第 {col} 列：{message}"
        else:
            full = message
        super().__init__(full)
