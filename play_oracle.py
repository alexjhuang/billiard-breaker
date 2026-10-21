"""Play the reference game (oracle.py) to sanity-check SPEC.md."""
import oracle
import screen

st = oracle.INIT


def stepper(left, right, reset):
    global st
    st = oracle.step(st, left, right, reset)
    return st, oracle.win(st), oracle.lose(st)


if __name__ == '__main__':
    screen.run(stepper)
