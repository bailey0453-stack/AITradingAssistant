"""Current recommendation calibration endpoint."""

from fastapi import APIRouter, Query

from app.services.current_calibration import calibration_for_confidence, calibration_text

router = APIRouter(prefix="/calibration-context", tags=["research"])


@router.get("")
def current_calibration(
    confidence: float = Query(..., ge=0.0, le=100.0),
    horizon: str = Query(default="4h", pattern="^(1h|4h|end_of_day|1d|2d|5d)$"),
) -> dict:
    result = calibration_for_confidence(confidence, horizon=horizon)
    result["summary"] = calibration_text(result)
    return result
