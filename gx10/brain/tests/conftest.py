import pytest

from marsrover_shared.schemas import Candidate


@pytest.fixture
def cand():
    def make(
        cand_id: str, x: float = 0.0, y: float = 0.0, score: float = 0.5, status: str = "new", **kw
    ):
        base = dict(
            cand_id=cand_id,
            run_id="r_1",
            station_id="s_0001",
            prop_ids=[f"p_{cand_id[2:]}"],
            x=x,
            y=y,
            score=score,
            novelty=0.5,
            visual=0.5,
            pickable=1.0,
            category_score=0.3,
            distance_m=0.0,
            category="float_rock",
            size_m=0.02,
            status=status,
            rank=-1,
            thumb_url="",
            reason="",
        )
        return Candidate(**{**base, **kw})

    return make
