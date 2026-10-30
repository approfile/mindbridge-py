# 第四步证据分层汇总

各层独立报告，不合并成功率。独立模型评审与生成使用同一供应商；人工未提交前不宣称已人工校准。

| 证据层 | 试次 | 不同场景 | 契约状态 |
| --- | ---: | ---: | --- |
| controlled | 19 | 19 | {'unknown': 13, 'pass': 6} |
| real-nominal | 53 | 41 | {'fail': 18, 'pass': 34, 'unknown': 1} |
| real-context-intervention | 3 | 3 | {'pass': 3} |

真实自然任务按划分：

| 划分 | 试次 | 契约通过 | 失败/未知 |
| --- | ---: | ---: | --- |
| development | 40 | 27 | unknown=1；其余未通过 |
| holdout | 13 | 7 | unknown=0；其余未通过 |

## RAGAS专项（12题，独立RAG子链路）

| 指标 | 全部12题均值 | 覆盖率 |
| --- | ---: | ---: |
| faithfulness | 0.5488869372462503 | 12/12 |
| answer_relevancy | 0.8284865928020068 | 12/12 |
| context_recall | 1.0 | 12/12 |
| context_relevance | 1.0 | 12/12 |

人工抽检状态：已收到人工提交

详见 docs/agent-layered-evaluation-v1.md；本页数字不是临床有效性、真实用户准确率或多模型对标结果。
