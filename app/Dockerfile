FROM python:3.7

RUN apt update && apt install -y default-mysql-client && apt clean

WORKDIR /opt/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

CMD [ "kopf", "run" ,"./app.py" ]