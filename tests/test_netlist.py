import random
import oracle
from hw import game
from hw.logic import evaluate


def random_states(n, seed):
    rng = random.Random(seed)
    st = oracle.INIT
    for _ in range(n):
        inp = (rng.random() < .3, rng.random() < .3, rng.random() < .01)
        yield st, inp
        st = oracle.step(st, *inp)
        if rng.random() < .02:  # jump to a random reachable-ish state
            small = rng.randrange(2)  # (px stays within the paddle's clamp, as in every reachable state)
            st = st._replace(bx=rng.randrange(32), by=rng.randrange(32), dx=rng.randrange(2), dy=rng.randrange(2),
                             px=rng.randrange(30 if small else 27), bricks=rng.getrandbits(32) if rng.random() < .9 else 0,
                             lives=rng.randrange(4), score=rng.randrange(250), speed=rng.randrange(4), ph=rng.randrange(8),
                             hits=rng.randrange(16), small=small, screen=rng.randrange(2))


def test_logic_matches_oracle():
    outs = game.build()
    for st, inp in random_states(3000, 1):
        vals = evaluate(outs, game.env_of(st, *inp))
        assert game.state_of(vals) == oracle.step(st, *inp), (st, inp)
        assert (vals['win'], vals['lose']) == (oracle.win(st), oracle.lose(st))


def test_switch_netlist_matches_oracle():
    from hw.lower import lower
    import netsim
    net = lower(game.build())
    for st, inp in random_states(2000, 2):
        vals = netsim.run(net, game.env_of(st, *inp))
        assert game.state_of(vals) == oracle.step(st, *inp), (st, inp)
        assert (vals['win'], vals['lose']) == (oracle.win(st), oracle.lose(st))
    print('   ', netsim.stats(net))
