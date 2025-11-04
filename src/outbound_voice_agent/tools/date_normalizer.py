from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional, TypedDict

from zoneinfo import ZoneInfo


class NormalizedDateTimeResult(TypedDict, total=False):
    ok: bool
    date: Optional[str]
    time: Optional[str]
    datetime_local: Optional[str]
    timezone: str
    confidence: float
    needs_confirmation: bool
    notes: Optional[str]


_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def normalize_datetime_local(
    text: str,
    *,
    current_local_date: str,
    current_local_time: str,
    timezone: str,
    language: str = "es-AR",
) -> NormalizedDateTimeResult:
    """
    Normalize Spanish and English date phrases for the collection bot.
    """
    normalized_text = (text or "").strip().lower()
    try:
        current_date = date.fromisoformat(current_local_date)
    except ValueError:
        return {
            "ok": False,
            "date": None,
            "time": None,
            "datetime_local": None,
            "timezone": timezone,
            "confidence": 0.0,
            "needs_confirmation": True,
            "notes": "Invalid current_local_date",
        }

    try:
        parsed_time = datetime.strptime(current_local_time, "%H:%M").time()
    except ValueError:
        parsed_time = None

    # 1) ISO date (YYYY-MM-DD)
    iso_match = _ISO_DATE_RE.search(normalized_text)
    if iso_match:
        try:
            iso_date = date.fromisoformat(iso_match.group(0))
            return _result(iso_date, parsed_time, timezone=timezone, confidence=0.95, needs_confirmation=False, notes="ISO date")
        except ValueError:
            pass

    # 2) Tomorrow / Mañana
    if any(phrase in normalized_text for phrase in ["tomorrow", "mañana"]):
        return _result(current_date + timedelta(days=1), parsed_time, timezone=timezone, confidence=0.9, needs_confirmation=False, notes="tomorrow")

    # 3) End of month / Fin de mes
    if any(phrase in normalized_text for phrase in ["end of month", "fin de mes", "a fin de mes"]):
        last_day = _last_day_of_month(current_date)
        return _result(last_day, parsed_time, timezone=timezone, confidence=0.9, needs_confirmation=False, notes="end of month")

    # 4) Month/Day parsing (e.g., "March 15th", "10 de marzo")
    months_map = {
        "january": 1, "enero": 1, "february": 2, "febrero": 2,
        "march": 3, "marzo": 3, "april": 4, "abril": 4,
        "may": 5, "mayo": 5, "june": 6, "junio": 6,
        "july": 7, "julio": 7, "august": 8, "agosto": 8,
        "september": 9, "septiembre": 9, "october": 10, "octubre": 10,
        "november": 11, "noviembre": 11, "december": 12, "diciembre": 12
    }

    # Match "March 15" or "15 de marzo"
    month_pattern = "|".join(months_map.keys())
    match = re.search(rf"\b({month_pattern})\b\s*(\d+)|(\d+)\s*(?:de\s+)?\b({month_pattern})\b", normalized_text)
    
    if match:
        m_name = match.group(1) or match.group(4)
        d_num = int(match.group(2) or match.group(3))
        m_num = months_map[m_name]
        
        try:
            # Assume current year, or next year if month has passed
            target_year = current_date.year
            if m_num < current_date.month:
                target_year += 1
            
            target_date = date(target_year, m_num, d_num)
            return _result(target_date, parsed_time, timezone=timezone, confidence=0.9, needs_confirmation=False, notes=f"specific date: {m_name} {d_num}")
        except ValueError:
            pass

    # 5) Weekdays (Spanish & English)
    weekdays_map = {
        "lunes": 0, "monday": 0,
        "martes": 1, "tuesday": 1,
        "miércoles": 2, "miercoles": 2, "wednesday": 2,
        "jueves": 3, "thursday": 3,
        "viernes": 4, "friday": 4,
        "sábado": 5, "sabado": 5, "saturday": 5,
        "domingo": 6, "sunday": 6
    }
    
    for word, day_idx in weekdays_map.items():
        if f" {word}" in f" {normalized_text}" or f"{word} " in f"{normalized_text} ":
            target_date = _next_weekday_on_or_after(current_date, weekday=day_idx)
            # If it's today, we usually mean next week in a collection context
            if target_date == current_date:
                target_date += timedelta(days=7)
                
            return _result(target_date, parsed_time, timezone=timezone, confidence=0.8, needs_confirmation=True, notes=f"weekday: {word}")

    return {
        "ok": False,
        "date": None,
        "time": None,
        "datetime_local": None,
        "timezone": timezone,
        "confidence": 0.0,
        "needs_confirmation": True,
        "notes": "Unsupported date phrase",
    }


def _result(local_date: date, local_time, *, timezone: str, confidence: float, needs_confirmation: bool, notes: str) -> NormalizedDateTimeResult:
    if local_time is None:
        datetime_local = None
        time_str = None
    else:
        try: tzinfo = ZoneInfo(timezone)
        except Exception: tzinfo = None
        dt = datetime.combine(local_date, local_time)
        if tzinfo is not None: dt = dt.replace(tzinfo=tzinfo)
        datetime_local = dt.isoformat()
        time_str = dt.strftime("%H:%M")

    return {
        "ok": True,
        "date": local_date.isoformat(),
        "time": time_str,
        "datetime_local": datetime_local,
        "timezone": timezone,
        "confidence": confidence,
        "needs_confirmation": needs_confirmation,
        "notes": notes,
    }


def _last_day_of_month(d: date) -> date:
    if d.month == 12: first_next = date(d.year + 1, 1, 1)
    else: first_next = date(d.year, d.month + 1, 1)
    return first_next - timedelta(days=1)


def _next_weekday_on_or_after(d: date, *, weekday: int) -> date:
    delta_days = (weekday - d.weekday()) % 7
    return d + timedelta(days=delta_days)