"""多周期趋势监控：15m / 1h / 4h 合成判断上涨或下跌趋势，附带失效位和支撑压力位。

方案文档：https://claude.ai/artifact/PwSkXNs6ptuce4cgeqtMC6
纯逻辑（series / levels / machine / buckets / backtest）只依赖标准库；engine 负责接入运行时。
"""
