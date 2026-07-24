import os
import sys
from collections import OrderedDict


class Base:
    def greet(self):
        return "base"


class Service(Base):
    def __init__(self, name):
        self.name = name

    def run(self):
        return helper(self.name)


def helper(value):
    return str(value).upper()


def main():
    svc = Service("x")
    return svc.run()
