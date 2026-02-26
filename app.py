from flask import Flask, render_template, session, request, send_file
from flask_socketio import SocketIO, emit, join_room, leave_room
import pandas as pd
import numpy as np
from collections import defaultdict
import copy
import time
import io
import uuid
import csv

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# ---------------- CONFIG ----------------
TEAM_SIZE = 15
PURSE = 120
ROLE_MIN = {"Bat":4, "Bowl":4, "AR":2, "WK":1}
TEAM_NAMES = [
    "DRS", "12 Angry Men", "Athaamle Vargeesu",
    "Lollipop XV", "Singapore Chithaps", "Forever Mama XV", 
    "Overdraft XV"
]
TEAM_COUNT = len(TEAM_NAMES)

# ---------------- GLOBAL STATE ----------------
auction_state = {
    "lots": [],
    "lot_idx": 0,
    "player_idx": 0,
    "phase": "LOTS",
    "unsold": [],
    "teams": {},
    "bid": 0,
    "leader": None,
    "history": [],
    "next_history": [],
    "ui_message": None,
    "ui_message_time": None,
    "initialized": False
}
connected_users = {}

# ---------------- HELPERS ----------------
def load_lots_from_excel():
    try:
        xls = pd.ExcelFile("players.xlsx")
        lots = []
        for sheet in xls.sheet_names:
            df = pd.read_excel(xls, sheet_name=sheet, header=1)
            df = df.dropna(subset=["Name"])
            df = df.sample(frac=1).reset_index(drop=True)
            lots.append({"name": sheet, "data": df.to_dict('records')})
        return lots
    except Exception as e:
        print(f"Error loading Excel: {e}")
        return []

def initialize_auction():
    if not auction_state["initialized"]:
        lots = load_lots_from_excel()
        auction_state.update({
            "lots": lots,
            "lot_idx": 0,
            "player_idx": 0,
            "phase": "LOTS",
            "unsold": [],
            "teams": {
                name: {
                    "players": [],
                    "spent": 0,
                    "purse": PURSE,
                    "overseas": 0,
                    "uncapped": 0,
                    "ipl": defaultdict(int)
                } for name in TEAM_NAMES
            },
            "bid": 0,
            "leader": None,
            "history": [],
            "next_history": [],
            "ui_message": None,
            "ui_message_time": None,
            "initialized": True
        })

def bid_increment(bid):
    if bid < 8: 
        return 0.5
    else:
        return 1

def role_counts(team):
    counts = {"Bat":0,"Bowl":0,"AR":0,"WK":0}
    for p in team["players"]:
        counts[p["role"]] += 1
    return counts

def can_bid(team, player, team_name, leader):
    # Purse check
    if team["purse"] < player["Base Price"]:
        return False, "Insufficient Purse"
    if leader == team_name:
        return False, "Already highest bidder"
    if len(team["players"]) >= TEAM_SIZE:
        return False, "Squad Full"
    if player["Nationality"]=="Overseas" and team["overseas"]>=6:
        return False, "Overseas Full"
    if team["ipl"][player["Team"]]>=4:
        return False, "IPL Quota Full"
    rc = role_counts(team)
    remaining = TEAM_SIZE - len(team["players"])
    current_uncapped = team["uncapped"]
    if remaining==1 and current_uncapped==0 and player["Uncapped"]!="Y":
        return False, "Fill Uncapped Quota"

    # Role minimum checks
    new_bat = rc["Bat"] + (1 if player["Role"]=="Bat" else 0)
    new_bowl = rc["Bowl"] + (1 if player["Role"]=="Bowl" else 0)
    new_ar = rc["AR"] + (1 if player["Role"]=="AR" else 0)
    new_wk = rc["WK"] + (1 if player["Role"]=="WK" else 0)
    new_uncapped = current_uncapped + (1 if player["Uncapped"]=="Y" else 0)
    min_needed = (
        max(0, ROLE_MIN["Bat"]-new_bat) +
        max(0, ROLE_MIN["Bowl"]-new_bowl) +
        max(0, ROLE_MIN["AR"]-new_ar) +
        max(0, ROLE_MIN["WK"]-new_wk) +
        max(0, 1-new_uncapped)
    )
    if min_needed > remaining-1:
        return False, "Combination Impossible"
    return True, ""

def current_player():
    if auction_state["phase"]=="LOTS":
        if auction_state["lot_idx"]<len(auction_state["lots"]):
            lot = auction_state["lots"][auction_state["lot_idx"]]["data"]
            if auction_state["player_idx"]<len(lot):
                return lot[auction_state["player_idx"]]
    else:
        if auction_state["player_idx"]<len(auction_state["unsold"]):
            return auction_state["unsold"][auction_state["player_idx"]]
    return None

def assign_player(team_name, player):
    t = auction_state["teams"][team_name]
    price = auction_state["bid"]
    t["players"].append({
        "name": player["Name"],
        "role": player["Role"],
        "team": player["Team"],
        "nat": player["Nationality"],
        "uncapped": player["Uncapped"],
        "price": price
    })
    t["spent"] += price
    t["purse"] -= price
    if player["Nationality"]=="Overseas":
        t["overseas"] += 1
    if player["Uncapped"]=="Y":
        t["uncapped"] +=1
    t["ipl"][player["Team"]] +=1

def broadcast_auction_update():
    player = current_player()
    teams = []
    for tname, t in auction_state["teams"].items():
        rc = role_counts(t)
        teams.append({
            "name": tname,
            "players": len(t["players"]),
            "spent": t["spent"],
            "purse": t["purse"],
            "overseas": t["overseas"],
            "uncapped": t["uncapped"],
            "roles": rc,
            "is_leader": auction_state["leader"]==tname
        })
    data = {
        "player": player,
        "current_bid": auction_state["bid"],
        "leader": auction_state["leader"],
        "teams": teams,
        "phase": auction_state["phase"],
        "lot_info": auction_state["lots"][auction_state["lot_idx"]]["name"] if auction_state["lots"] else "",
        "ui_message": auction_state.get("ui_message")
    }
    socketio.emit('auction_update', data, room='auction')

# ---------------- ROUTES ----------------
@app.route('/')
def index(): return render_template('role_select.html')
@app.route('/auctioneer')
def auctioneer():
    session['role']='auctioneer'; session['user_id']=str(uuid.uuid4())
    initialize_auction()
    return render_template('auction.html', role='auctioneer')
@app.route('/player')
def player():
    session['role']='player'; session['user_id']=str(uuid.uuid4())
    initialize_auction()
    return render_template('auction.html', role='player')

# ---------------- SOCKET EVENTS ----------------
@socketio.on('connect')
def on_connect():
    user_id=session.get('user_id'); role=session.get('role','player')
    if user_id:
        connected_users[user_id] = {'role': role, 'session_id': request.sid}
        join_room('auction')
        broadcast_auction_update()
        emit('user_connected', {'role':role, 'total_users':len(connected_users)}, room='auction')

@socketio.on('disconnect')
def on_disconnect():
    user_id=session.get('user_id')
    if user_id in connected_users: del connected_users[user_id]; leave_room('auction')

@socketio.on('place_bid')
def handle_bid(data):
    if session.get('role')!='auctioneer': emit('error',{'message':'Only auctioneer can control bidding'}); return
    team_name=data.get('team'); player=current_player()
    if not player: return
    team=auction_state["teams"][team_name]
    ok,msg=can_bid(team,player,team_name,auction_state["leader"])
    if ok:
        new_bid = player["Base Price"] if auction_state["bid"]==0 else auction_state["bid"]+bid_increment(auction_state["bid"])
        if new_bid<=team["purse"]:
            auction_state["bid"]=new_bid
            auction_state["leader"]=team_name
            auction_state["history"].append((team_name,new_bid))
            broadcast_auction_update()

@socketio.on('undo_bid')
def handle_undo_bid():
    if session.get('role')!='auctioneer': emit('error',{'message':'Only auctioneer can undo bids'}); return
    if auction_state["history"]:
        auction_state["history"].pop()
        if auction_state["history"]: auction_state["leader"],auction_state["bid"]=auction_state["history"][-1]
        else: auction_state["leader"],auction_state["bid"]=None,0
        broadcast_auction_update()

@socketio.on('undo_next_player')
def handle_undo_next_player():
    if session.get('role') != 'auctioneer':
        emit('error', {'message': 'Only auctioneer can undo next player'})
        return
    
    if auction_state["next_history"]:
        snap = auction_state["next_history"].pop()
        auction_state["lot_idx"] = snap["lot_idx"]
        auction_state["player_idx"] = snap["player_idx"]
        auction_state["phase"] = snap["phase"]
        auction_state["bid"] = snap["bid"]
        auction_state["leader"] = snap["leader"]
        auction_state["history"] = snap["history"]
        auction_state["teams"] = snap["teams"]
        auction_state["unsold"] = snap["unsold"]

        auction_state["ui_message"] = "Reverted to previous player"
        auction_state["ui_message_time"] = time.time()
        broadcast_auction_update()

@socketio.on('next_player')
def handle_next_player():
    if session.get('role') != 'auctioneer':
        emit('error', {'message': 'Only auctioneer can advance'})
        return

    player = current_player()

    # If no player and still in LOTS, force phase switch
    if not player and auction_state["phase"] == "LOTS":
        auction_state["lot_idx"] += 1
        auction_state["player_idx"] = 0

        if auction_state["lot_idx"] >= len(auction_state["lots"]):
            auction_state["phase"] = "UNSOLD"
            auction_state["player_idx"] = 0

        player = current_player()

    # If still no player, check UNSOLD completion
    if not player and auction_state["phase"] == "UNSOLD":
        all_full = all(
            len(t["players"]) >= TEAM_SIZE
            for t in auction_state["teams"].values()
        )

        if all_full:
            auction_state["ui_message"] = "Auction Completed - All Teams Full"
            auction_state["ui_message_time"] = time.time()
            broadcast_auction_update()
            return
        else:
            auction_state["player_idx"] = 0
            player = current_player()

    if not player:
        broadcast_auction_update()
        return

    # Save snapshot for undo
    auction_state["next_history"].append({
        "lot_idx": auction_state["lot_idx"],
        "player_idx": auction_state["player_idx"],
        "phase": auction_state["phase"],
        "bid": auction_state["bid"],
        "leader": auction_state["leader"],
        "history": copy.deepcopy(auction_state["history"]),
        "teams": copy.deepcopy(auction_state["teams"]),
        "unsold": copy.deepcopy(auction_state["unsold"])
    })

    # Assign or move to unsold
    if auction_state["leader"]:
        assign_player(auction_state["leader"], player)
    else:
        if auction_state["phase"] == "LOTS":
            auction_state["unsold"].append(player)
            auction_state["ui_message"] = f"{player['Name']} moved to Unsold List"
            auction_state["ui_message_time"] = time.time()

    # Reset bidding state
    auction_state["bid"] = 0
    auction_state["leader"] = None
    auction_state["history"] = []

    # Move index forward
    auction_state["player_idx"] += 1

    broadcast_auction_update()

@socketio.on('reset_auction')
def handle_reset():
    if session.get('role')!='auctioneer': emit('error',{'message':'Only auctioneer can reset auction'}); return
    auction_state["initialized"]=False; initialize_auction(); broadcast_auction_update()

@socketio.on('request_summary')
def handle_summary():
    rows=[]
    for tname,t in auction_state["teams"].items():
        for p in t["players"]:
            rows.append({"Auction Team":tname, **p})
    si = io.StringIO()
    writer = csv.DictWriter(si, fieldnames=rows[0].keys() if rows else [])
    writer.writeheader(); writer.writerows(rows)
    emit('download_csv', si.getvalue())

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)