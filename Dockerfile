FROM python:3.11-slim-buster

ENV BOT_TOKEN       ''
ENV SHEETS_ACC_JSON ''
ENV SHEETS_LINK     ''
ENV SCHELDUE_TIME   ''
ENV DEBUG           'false'

WORKDIR /python-docker

COPY requirements.txt .
RUN pip3 install -r requirements.txt

COPY *.py ./

CMD [ "python3", "-u", "main.py"]