"""Dashboard 100% server-side. Cero JS obligatorio."""
import json, os, sys, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd
from flask import Flask, redirect, request
from sqlalchemy import text
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from config.database import engine
from dashboard.display import league_name, market_name, match_name, result_label
app = Flask(__name__)
RESOLVED = ("win","loss","push","half_win","half_loss")
WIN_LIKE = ("win","half_win")
PF = {"win":1.0,"half_win":0.5,"loss":-1.0,"half_loss":-0.5,"push":0.0}
GH_REPO = "AldairG16/Sport_Betting_Model"
MX_TZ = "America/Mexico_City"