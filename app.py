from flask import Flask, render_template, request, session, redirect, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
import pandas as pd
import numpy as np
from collections import defaultdict
import copy
import time
import uuid

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# ---------------- CONFIG ----------------
TEAM_SIZE = 15
PURSE = 120
ROLE_MIN = {"Bat":4, "Bowl":4, "AR":2, "WK":1}
TEAM_NAMES = [
    "Chennai Super Kings", "Mumbai Indians", "Royal Challengers Bangalore",
    "Kolkata Knight Riders", "Delhi Capitals", "Rajasthan Royals", 
    "Sunrisers Hyderabad", "Punjab Kings"
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

# ---------------- HELPER FUNCTIONS ----------------
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
            "initialized": True
        })

def bid_increment(bid):
    if bid < 8: return 0.5
    if bid < 12: return 0.75
    return 1

def role_counts(team):
    counts = {"Bat":0,"Bowl":0,"AR":0,"WK":0}
    for p in team["players"]:
        counts[p["role"]] += 1
    return counts

def can_bid(team, player, team_name, leader):
    if team["purse"] < player["Base Price"]:
        return False, "Insufficient Purse"
    
    if leader == team_name:
        return False, "Already highest bidder"
    
    current_count = len(team["players"])
    if current_count >= TEAM_SIZE:
        return False, "Squad Full"
    
    if player["Nationality"] == "Overseas" and team["overseas"] >= 6:
        return False, "Overseas Full"
    
    if team["ipl"][player["Team"]] >= 4:
        return False, "IPL Quota Full"
    
    rc = role_counts(team)
    remaining_slots = TEAM_SIZE - current_count
    
    if remaining_slots == 1 and team["uncapped"] == 0:
        if player["Uncapped"] != "Y":
            return False, "Fill Uncapped Quota"
    
    # Additional validation logic here...
    return True, ""

def current_player():
    if auction_state["phase"] == "LOTS":
        if auction_state["lot_idx"] < len(auction_state["lots"]):
            lot_data = auction_state["lots"][auction_state["lot_idx"]]["data"]
            if auction_state["player_idx"] < len(lot_data):
                return lot_data[auction_state["player_idx"]]
    else:
        if auction_state["player_idx"] < len(auction_state["unsold"]):
            return auction_state["unsold"][auction_state["player_idx"]]
    return None

def assign_player(team_name, player):
    team = auction_state["teams"][team_name]
    price = auction_state["bid"]
    
    team["players"].append({
        "name": player["Name"],
        "role": player["Role"],
        "team": player["Team"],
        "nat": player["Nationality"],
        "uncapped": player["Uncapped"],
        "price": price
    })
    
    team["spent"] += price
    team["purse"] -= price
    
    if player["Nationality"] == "Overseas":
        team["overseas"] += 1
    if player["Uncapped"] == "Y":
        team["uncapped"] += 1
    
    team["ipl"][player["Team"]] += 1

def broadcast_auction_update():
    """Send current auction state to all connected clients"""
    player = current_player()
    
    # Prepare team summaries
    team_summaries = []
    for tname, team in auction_state["teams"].items():
        rc = role_counts(team)
        team_summaries.append({
            "name": tname,
            "players": len(team["players"]),
            "spent": team["spent"],
            "purse": team["purse"],
            "overseas": team["overseas"],
            "uncapped": team["uncapped"],
            "roles": rc,
            "is_leader": auction_state["leader"] == tname
        })
    
    update_data = {
        "player": player,
        "current_bid": auction_state["bid"],
        "leader": auction_state["leader"],
        "teams": team_summaries,
        "phase": auction_state["phase"],
        "lot_info": auction_state["lots"][auction_state["lot_idx"]]["name"] if auction_state["lots"] else "",
        "ui_message": auction_state.get("ui_message")
    }
    
    socketio.emit('auction_update', update_data, room='auction')

# ---------------- ROUTES ----------------
@app.route('/')
def index():
    return render_template('role_select.html')

@app.route('/auctioneer')
def auctioneer():
    session['role'] = 'auctioneer'
    session['user_id'] = str(uuid.uuid4())
    initialize_auction()
    return render_template('auction.html', role='auctioneer')

@app.route('/player')
def player():
    session['role'] = 'player'
    session['user_id'] = str(uuid.uuid4())
    initialize_auction()
    return render_template('auction.html', role='player')

# ---------------- SOCKET EVENTS ----------------
@socketio.on('connect')
def on_connect():
    user_id = session.get('user_id')
    role = session.get('role', 'player')
    
    if user_id:
        connected_users[user_id] = {
            'role': role,
            'session_id': request.sid
        }
        
        join_room('auction')
        
        # Send initial auction state
        broadcast_auction_update()
        
        # Notify about user connection
        emit('user_connected', {
            'role': role,
            'total_users': len(connected_users)
        }, room='auction')

@socketio.on('disconnect')
def on_disconnect():
    user_id = session.get('user_id')
    if user_id in connected_users:
        del connected_users[user_id]
        leave_room('auction')

@socketio.on('place_bid')
def handle_bid(data):
    # Only allow auctioneer or validate bidding rights
    if session.get('role') != 'auctioneer':
        emit('error', {'message': 'Only auctioneer can control bidding'})
        return
    
    team_name = data.get('team')
    player = current_player()
    
    if not player:
        return
    
    team = auction_state["teams"][team_name]
    can_bid_result, msg = can_bid(team, player, team_name, auction_state["leader"])
    
    if can_bid_result:
        new_bid = player["Base Price"] if auction_state["bid"] == 0 else auction_state["bid"] + bid_increment(auction_state["bid"])
        
        if new_bid <= team["purse"]:
            auction_state["bid"] = new_bid
            auction_state["leader"] = team_name
            auction_state["history"].append((team_name, new_bid))
            
            broadcast_auction_update()

@socketio.on('undo_bid')
def handle_undo_bid():
    if session.get('role') != 'auctioneer':
        emit('error', {'message': 'Only auctioneer can undo bids'})
        return
    
    if auction_state["history"]:
        auction_state["history"].pop()
        if auction_state["history"]:
            auction_state["leader"], auction_state["bid"] = auction_state["history"][-1]
        else:
            auction_state["leader"], auction_state["bid"] = None, 0
        
        broadcast_auction_update()

@socketio.on('next_player')
def handle_next_player():
    if session.get('role') != 'auctioneer':
        emit('error', {'message': 'Only auctioneer can advance to next player'})
        return
    
    player = current_player()
    if not player:
        return
    
    # Save state for undo
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
    
    # Assign or mark as unsold
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
    
    # Advance to next player
    auction_state["player_idx"] += 1
    
    if auction_state["phase"] == "LOTS":
        if auction_state["player_idx"] >= len(auction_state["lots"][auction_state["lot_idx"]]["data"]):
            auction_state["lot_idx"] += 1
            auction_state["player_idx"] = 0
            
            if auction_state["lot_idx"] >= len(auction_state["lots"]):
                auction_state["phase"] = "UNSOLD"
                auction_state["player_idx"] = 0
    else:
        if auction_state["player_idx"] >= len(auction_state["unsold"]):
            auction_state["player_idx"] = 0
    
    broadcast_auction_update()

@socketio.on('reset_auction')
def handle_reset():
    if session.get('role') != 'auctioneer':
        emit('error', {'message': 'Only auctioneer can reset auction'})
        return
    
    # Reset global state
    auction_state["initialized"] = False
    initialize_auction()
    broadcast_auction_update()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)