FROM python:3.12-slim

WORKDIR /srv
COPY ops_server.py /srv/ops_server.py
COPY data /srv/data

ENV OPS_HOST=0.0.0.0
ENV OPS_PORT=8642
EXPOSE 8642

CMD ["python3", "/srv/ops_server.py"]
