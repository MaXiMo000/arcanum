"""Classrooms: a teacher's statements and results, streamed live to everyone who joins with the code.

A teacher opens a classroom and gets a short code to share and a secret token.
Each statement the teacher runs is posted to the classroom with a summary of
its results, and so is the lesson step the teacher is on; students who joined
with the code receive both over a WebSocket as they happen (and the current
state when they join), and can run the same statement on their own database
or follow the lesson step by step. The one thing students send is a question,
which only the teacher can read; nothing reaches the other students.

Only the token posts or ends a classroom; it is stored hashed and compared in
constant time. Classrooms live in memory, are capped in number, size and
history, and close after sitting idle.
"""

import hashlib
import hmac
import secrets
import time
from collections import deque

from .sessions import SessionError

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: codes are read aloud and typed
CODE_LENGTH = 6
HISTORY = 50  # posts a classroom keeps for students who join late
QUESTIONS = 100  # questions a classroom keeps for its teacher


def normalize(code):
    return code.strip().upper().replace("-", "").replace(" ", "")


def _digest(token):
    return hashlib.sha256(token.encode()).digest()


class Classroom:
    def __init__(self, code, token, title, owner):
        self.code, self.title, self.owner = code, title, owner
        self._token = _digest(token)
        self.history = deque(maxlen=HISTORY)
        self.seq = 0
        self.students = set()  # Subscribers
        self.lesson = None  # {"id", "step"}: the lesson step the teacher is on, if any
        self.questions = deque(maxlen=QUESTIONS)
        self.asked = 0
        self.last_used = time.monotonic()

    def is_teacher(self, token):
        return token is not None and hmac.compare_digest(_digest(token), self._token)

    def post(self, item):
        self.seq += 1
        item = {"seq": self.seq, "at": time.time(), **item}
        self.history.append(item)
        self._send({"type": "post", "post": item})
        return item

    def follow(self, lesson):
        """The teacher moved to a lesson step (or left the lessons, with None)."""
        self.lesson = lesson
        self._send({"type": "lesson", "lesson": lesson})

    def ask(self, text, name):
        self.asked += 1
        question = {"id": self.asked, "at": time.time(), "text": text, "name": name}
        self.questions.append(question)
        self.last_used = time.monotonic()
        return question

    def dismiss(self, question_id):
        self.questions = deque((q for q in self.questions if q["id"] != question_id), maxlen=QUESTIONS)

    def _send(self, message):
        self.last_used = time.monotonic()
        for student in list(self.students):
            student.offer([message])

    def summary(self, teacher=False):
        """What anyone with the code sees; the teacher also gets the questions."""
        out = {"code": self.code, "title": self.title, "students": len(self.students), "history": list(self.history),
               "lesson": self.lesson}
        if teacher:
            out["questions"] = list(self.questions)
        return out

    def close(self):
        for student in list(self.students):
            student.close()
        self.students.clear()


class ClassroomManager:
    def __init__(self, settings):
        self.settings = settings
        self.rooms = {}

    def create(self, client, title):
        mine = sum(1 for room in self.rooms.values() if room.owner == client)
        if len(self.rooms) >= self.settings.max_classrooms or mine >= self.settings.max_classrooms_per_client:
            self.reap()
            if len(self.rooms) >= self.settings.max_classrooms or mine >= self.settings.max_classrooms_per_client:
                raise SessionError(503, "ClassroomsFull", "no more classrooms can be opened right now; try later")
        while (code := "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))) in self.rooms:
            pass
        token = secrets.token_urlsafe(24)
        room = self.rooms[code] = Classroom(code, token, title, client)
        return room, token

    def get(self, code):
        room = self.rooms.get(normalize(code))
        if room is None:
            raise SessionError(404, "UnknownClassroom", "no classroom has that code; check it with your teacher")
        return room

    def teacher(self, code, token):
        """The classroom, if `token` is its teacher's."""
        room = self.get(code)
        if not room.is_teacher(token):
            raise SessionError(403, "NotTheTeacher", "only the teacher who opened this classroom can do that")
        return room

    def end(self, code, token):
        room = self.teacher(code, token)
        room.close()
        del self.rooms[room.code]

    def reap(self):
        """Close classrooms nobody has posted to or joined for the classroom TTL."""
        now = time.monotonic()
        for code, room in list(self.rooms.items()):
            if now - room.last_used > self.settings.classroom_ttl and not room.students:
                room.close()
                del self.rooms[code]

    def close_all(self):
        for room in self.rooms.values():
            room.close()
        self.rooms.clear()
