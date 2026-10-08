FROM python:3.13-slim

WORKDIR /app

# 依赖单独一层，源码改动不触发重装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor/ monitor/
COPY assets/ assets/
COPY config.example.yaml .

# 数据目录（SQLite + config.yaml），运行时挂卷持久化
RUN mkdir -p data
VOLUME ["/app/data"]

EXPOSE 8787

# 容器内以 data/config.yaml 启动；首次启动若无则复制 example 生成
CMD ["sh", "-c", "test -f data/config.yaml || cp config.example.yaml data/config.yaml; python -m monitor data/config.yaml"]
