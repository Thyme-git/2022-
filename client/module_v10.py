import torch.nn as nn
import torch
import torch.nn.functional as F
from resp import *
from req import *
from base import *
from collections import deque
import random

import os
import json
from threading import Thread

from ui import UI
import subprocess

from time import sleep


'''
   characterState: {
        "x": 5, "y": -10, 
        "playerID": 1, 
        "characterID": 0, 
        "direction": 3, 
        "color": 2, 
        "hp": 50, 
        "moveCD": 4, 
        "moveCDLeft": 0, 
        "isAlive": true, 
        "isSneaky": false, 
        "isGod": false, 
        "rebornTimeLeft": 0, 
        "godTimeLeft": 0, 

        "slaveWeapon": {"weaponType": 1, "attackCD": 30, "attackCDLeft": 0}, 
        "masterWeapon": {"weaponType": 1, "attackCD": 3, "attackCDLeft": 0}}

'''

'''参考main.py里的refreshUI()'''
def actionResp2info(packet: PacketResp):
    '''将actionResp转换成reward, state, done, slavryCD, death'''
    if packet.type == PacketType.GameOver:
        return packet.data.scores[-1], torch.zeros((5*16*16+10,)), True, False, False

    data = packet.data
    score = data.score
    playerId = data.playerID
    kill = data.kill
    color = data.color
    blocks =  data.map.blocks

    total_reward = score
    # total_reward = score
    done = False

    # [0]:颜色信息， [1] 墙体信息， [2]:人物, [3]:buff, [4]:slaveryweapon
    state = torch.zeros((5, 16, 16))
    player_state = torch.zeros((10,))

    slavryCD = False
    death = False

    for block in blocks:
        state[0][block.x][-block.y] = 1.0 if block.color == color else -1.0
        state[1][block.x][-block.y] = 1.0 if block.valid else 0.0
        if len(block.objs):
            for obj in block.objs:
                if obj.type == ObjType.Character:
                    if obj.status.playerID == playerId:
                        player_state[0] = obj.status.hp
                        player_state[1] = obj.status.moveCD
                        player_state[2] = obj.status.moveCDLeft
                        player_state[3] = obj.status.rebornTimeLeft
                        player_state[4] = obj.status.godTimeLeft
                        if not obj.status.isAlive:
                            death = True
                        if obj.status.slaveWeapon.attackCDLeft == 0:
                            slavryCD = True
                        state[2][block.x][-block.y] = 1
                    else:
                        player_state[5] = obj.status.hp
                        player_state[6] = obj.status.moveCD
                        player_state[7] = obj.status.moveCDLeft
                        player_state[8] = obj.status.rebornTimeLeft
                        player_state[9] = obj.status.godTimeLeft
                        state[2][block.x][-block.y] = -1
    
                elif obj.type == ObjType.Item:
                    state[3][block.x][-block.y] += obj.status.buffType + 1 
   
                elif obj.type == ObjType.SlaveWeapon:
                    if obj.status.playerID == playerId:
                        state[4][block.x][-block.y] = 1
                    else:
                        state[4][block.x][-block.y] = -1

    return total_reward, \
           torch.concat([state.view(-1), player_state]), \
           done, \
           slavryCD, \
           death


'''
    action space : [0, 5]
        0 --> 5 : turn around 0-5 and move and use master/slavery weapon
'''
class Qnet(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Sequential(
            nn.Conv2d(2, 4, 3, 1, 1),
            nn.BatchNorm2d(4),
            nn.ReLU()
        )

        self.conv2 = nn.Sequential(
            nn.Conv2d(4, 8, 3, 1, 1),
            nn.BatchNorm2d(8),
            nn.ReLU()
        )

        self.maxpool = nn.MaxPool2d(2, 2)

        self.fc1 = nn.Linear(8*8*8 + 5*16*16+10, 1024)
        self.fc2 = nn.Linear(1024, 256)
        self.fc_A = nn.Linear(256, 6)
        self.fc_V = nn.Linear(256, 1)
    
    def forward(self, x):
        map = x[:,:-10].view((x.size()[0], 5, 16, 16))[:, :2]

        map_feat = self.maxpool(self.conv2(self.conv1(map)))

        map_feat = map_feat.view(map_feat.size()[0], -1)

        x = x.view((x.size()[0], -1))
        A = self.fc_A(F.relu(self.fc2(F.relu(self.fc1(torch.cat([map_feat, x], dim = 1))))))
        V = self.fc_V(F.relu(self.fc2(F.relu(self.fc1(torch.cat([map_feat, x], dim = 1))))))
        Q = V + A - A.mean(1).view(-1, 1)
        return Q
    

class Config:
    n_epoch = 6000
    action_space = 6
    lr = 1e-3
    gamma = 0.99
    epsilon = 0.2
    target_update = 100
    buffer_size = 50000
    minimal_size = 10000
    batch_size = 256
    device = torch.device("cuda") if torch.cuda.is_available() else torch.device(
    "cpu")


class Memory:
    def __init__(self, buffer_size):
        self.buffer = deque(maxlen=buffer_size)
    
    def push(self, state, action, reward, next_state, done):  # 将数据加入buffer
        self.buffer.append((state, action, reward, next_state, done))
    
    def size(self):
        return len(self.buffer)
    
    def sample(self, batch_size):
        transition = random.sample(self.buffer, batch_size)
        states, actions, rewards, next_states, dones = zip(*transition)
        return torch.stack(states, 0),\
               torch.tensor(actions),\
               torch.tensor(rewards), \
               torch.stack(next_states, 0), \
               torch.tensor(dones).to(torch.float)


class Agent:
    def __init__(self, config:Config):
        self.config = config

        self.epsilon = config.epsilon
    
        self.memory = Memory(config.buffer_size)

        self.q_net = Qnet().to(self.config.device)
        self.target_q_net = Qnet().to(self.config.device)
        
        self.criterion = torch.nn.MSELoss()

        self.optimizer = torch.optim.Adam(self.q_net.parameters(),lr=config.lr)

        self.count = 0

        self.lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(self.optimizer, T_max=6000)

    def act(self, state):
        prob = self.q_net(state.view((1, *state.size())).to(self.config.device))
        action = torch.distributions.Categorical(F.softmax(prob.view(-1))).sample().item()
        return action

    def memorize(self, state, action, reward, next_state, done):
        self.memory.push(state, action, reward, next_state, done)

    def learn(self):
        if (self.memory.size() < self.config.minimal_size):
            return
        
        states, actions, rewards, next_states, dones = self.memory.sample(self.config.batch_size)
        states = states.to(self.config.device)
        actions = actions.to(self.config.device)
        rewards = rewards.to(self.config.device)
        next_states = next_states.to(self.config.device)
        dones = dones.to(self.config.device)

        q_values = self.q_net(states).gather(1, actions.view((-1, 1)))

        max_actions = self.q_net(next_states).argmax(1)

        next_q_values = self.target_q_net(next_states).gather(1, max_actions.view((-1, 1)))

        q_targets = rewards.view((-1, 1)) + self.config.gamma*next_q_values*(1-dones.view((-1, 1)))

        loss = self.criterion(q_values.view(-1), q_targets.view(-1))

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()
        self.lr_scheduler.step()
        
        if self.count % self.config.target_update == 0:
            self.target_q_net.load_state_dict(self.q_net.state_dict())
        
        self.count += 1

    def save(self, path = './model.pt'):
        torch.save(self.q_net.state_dict(), path)

    def load(self, path = './model.pt'):
        self.q_net.load_state_dict(torch.load(path))
        self.target_q_net.load_state_dict(torch.load(path))


int2action = {
    -1: (ActionType.Move, EmptyActionParam()),
    0: (ActionType.TurnAround, TurnAroundActionParam(Direction.Above)),
    1: (ActionType.TurnAround, TurnAroundActionParam(Direction.TopRight)),
    2: (ActionType.TurnAround, TurnAroundActionParam(Direction.BottomRight)),
    3: (ActionType.TurnAround, TurnAroundActionParam(Direction.Bottom)),
    4: (ActionType.TurnAround, TurnAroundActionParam(Direction.BottomLeft)),
    5: (ActionType.TurnAround, TurnAroundActionParam(Direction.TopLeft)),
    7: (ActionType.MasterWeaponAttack, EmptyActionParam()),
    8: (ActionType.SlaveWeaponAttack, EmptyActionParam()),
}
def action2actionReq(action, characterID, slaveryCD):
    actions = [ActionReq(characterID,*int2action[action])]
    actions.append(ActionReq(characterID,*int2action[-1]))
    if slaveryCD:
        actions.append(ActionReq(characterID,*int2action[8]))
    else:
        actions.append(ActionReq(characterID,*int2action[7]))

    return actions


def refreshUI(ui: UI, packet: PacketResp):
    """Refresh the UI according to the response."""
    data = packet.data
    if packet.type == PacketType.ActionResp:
        ui.playerID = data.playerID
        ui.color = data.color
        ui.characters = data.characters
        ui.score = data.score
        ui.kill = data.kill

        for block in data.map.blocks:
            if len(block.objs):
                ui.block = {
                    "x": block.x,
                    "y": block.y,
                    "color": block.color,
                    "valid": block.valid,
                    "obj": block.objs[-1].type,
                    "data": block.objs[-1].status,
                }
            else:
                ui.block = {
                    "x": block.x,
                    "y": block.y,
                    "color": block.color,
                    "valid": block.valid,
                    "obj": ObjType.Null,
                }
    subprocess.run(["clear"])
    ui.display()


'''
    坐标(x, y):

    方向s:      到达:
        0   --> (x-1, y-1)
        1   --> (x-1,   y)
        2   --> (  x, y+1)
        3   --> (x+1, y+1)
        4   --> (x+1,   y)
        5   --> (  x, y-1)
'''
dir = {0:(lambda x, y : (x-1, y-1)),
           1:(lambda x, y : (x-1,   y)),
           2:(lambda x, y : (  x, y+1)),
           3:(lambda x, y : (x+1, y+1)),
           4:(lambda x, y : (x+1,   y)),
           5:(lambda x, y : (  x, y-1))}
grayLocations = [(13, 7), (13, 8), (14, 7), (14, 8), (14, 9), (15, 8), (15, 9), (13,13), (13, 14), (14, 13), (14, 14), (14, 15), (15, 14), (15, 15)]
# 撞墙扣除CD
'''吃到了buff:0, 呆在自己的领地上:1, 进入灰色地带:2, 撞墙:3, 其他:4'''
def getNextLoc(action_int, state, playId=1, color=1):
    action_int = action_int%7
    if action_int == 6:
        return 4
    obs = state[:-10].view((5, 16, 16))
    index = torch.nonzero(obs[2] == playId)
    if index.size()[0] == 0:
        return 4
    x, y = index[0][0], index[0][1]
    
    next_x, next_y = dir[action_int](x, y)
    if next_x < 0 or next_y < 0 or next_x > 15 or next_y > 15 or obs[1][next_x][next_y] == 0:
        return 3
    if (next_x, next_y) in grayLocations:
        return 2
    if obs[0][next_x][next_y] == color:
        return 1
    if obs[3][next_x][next_y] > 0:
        return 0
    return 4


# 修改config.json来改变端口实现无缝衔接训练
def changePort(port:int = 9999):
    with open('./config.json') as f:
        config_json = json.load(f)
        config_json['Port'] = port
        f.close()
    with open('./config.json', 'w') as f:
        json.dump(config_json, f)
        f.close()


UserInterface = 0
def main():
    '''🤡🥵🤡🥵🤡🥵🤡🥵🤡🥵🤡🥵🤡🥵🤡🥵🤡🥵🤡🥵🤡🥵🤡'''
    ports = [9991, 9992, 9993, 9994, 9995, 9996, 9997, 9998, 9999]
    # ports = [9997, 9998, 9999
    if UserInterface:
        ui = UI()
    
    config = Config()
    agent = Agent(config)
    # agent.load('./model_v10.pt')

    for epoch in range(config.n_epoch):
        port = ports[epoch%len(ports)]
        changePort(port)
        serverThead = Thread(target=os.system, args=('./seedcupServer',))
        botThead = Thread(target=os.system, args=('./bot',))
        
        serverThead.start()
        sleep(1)
        botThead.start()
        sleep(1)

        scores = 0

        connected = False

        with Client() as client:
                client.port = port
                client.connect()

                init_req = InitReq(MasterWeaponType.Durian, SlaveWeaponType.Cactus)
                init_packet = PacketReq(PacketType.InitReq, init_req)
                client.send(init_packet)


                resp = client.recv()
                if UserInterface:
                    refreshUI(ui, resp)
                
                total_reward, state, done, slaveryCD, death = actionResp2info(resp)
                check = 4

                characterId = resp.data.characters[-1].characterID

                while resp.type != PacketType.GameOver:
                    action_int = agent.act(state)
                    actionReq = action2actionReq(action_int, characterId, slaveryCD)
                    actionPacket = PacketReq(PacketType.ActionReq, actionReq)
                    client.send(actionPacket)

                    resp = client.recv()
                    if UserInterface:
                        refreshUI(ui, resp)
                    
                    next_total_reward, next_state, done, slaveryCD, next_death = actionResp2info(resp)

                    reward = next_total_reward-total_reward
                    next_check = getNextLoc(action_int, state)
                    if next_check == 0:
                        reward += 50
                    if check != 1 and next_check == 1:
                        reward -= 30
                    if check != 2 and next_check == 2:
                        reward -= 30
                    if check == 2 and next_check != 2:
                        reward += 20
                    if next_check == 3:
                        reward -= 30
                    if not death and next_death:
                        reward -= 100
                    
                    agent.memorize(state, action_int,reward, next_state, done)
                    agent.learn()

                    state = next_state
                    total_reward = next_total_reward
                    death = next_death
                    check = next_check

                agent.save('./model_v10.pt')
                scores = resp.data.scores
                with open('./score_logs_v10', 'a') as f:
                    f.write(str(scores) + '\n')
                    f.close()


        serverThead.join()
        sleep(1)
        botThead.join()
        sleep(1)


if __name__ == '__main__':
    from main import Client
    main()