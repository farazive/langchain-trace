FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8000

# No --reload: the reloader forks, and the child process loses the
# instrumentation set up in the parent.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
