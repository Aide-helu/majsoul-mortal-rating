"""按输入玩家的上家、对家、下家分别汇总最近对局的 Mortal 评分。

用法：python mahjong_rating_rival.py --name "白马#" --games 20 --mode 玉
结果：rating_out/<玩家昵称>_rival/{上家,对家,下家}/
跑谱、缓存、绘图逻辑与 mahjong_rating.py 共用。
"""

from mahjong_rating import main


if __name__ == "__main__":
    raise SystemExit(main(rival=True))
