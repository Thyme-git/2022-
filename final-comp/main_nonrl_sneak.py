import torch
from resp import *
from req import *
from base import *


from ui import UI
import subprocess

from functools import partial

'''sneak and unsneak'''


def actionResp2info(packet: PacketResp):
    '''将actionResp转换成reward, state, done, slavryCD'''
    if packet.type == PacketType.GameOver:
        return packet.data.scores[-1], torch.zeros((5 * 16 * 16 + 10,)), True, False

    data = packet.data
    playerId = data.playerID
    color = data.color
    blocks = data.map.blocks
    done = False

    '''1:标记无视野'''
    # [0]:颜色信息， [1] 墙体信息， [2]:人物, [3]:buff, [4]:slavery_weapon    -10标记无视野
    state = torch.zeros((5, 24, 24))
    player_state = torch.zeros((10,))

    slaveryCD = False

    '''2:有可能看不到敌人'''
    CON = []
    PRO = []

    for block in blocks:
        state[0][block.x][-block.y] = 1.0 if block.color == color else -1.0
        state[1][block.x][-block.y] = 1.0 if block.valid else 0.0
        if len(block.objs):
            for obj in block.objs:
                if obj.type == ObjType.Character:
                    if obj.status.playerID == playerId:
                        player_state[0] = obj.status.hp
                        player_state[1] = obj.status.masterWeapon.attackCDLeft
                        player_state[2] = obj.status.slaveWeapon.attackCDLeft
                        player_state[3] = obj.status.rebornTimeLeft
                        player_state[4] = obj.status.godTimeLeft
                        if obj.status.slaveWeapon.attackCDLeft == 0:
                            slaveryCD = True
                        state[2][block.x][-block.y] = 1
                        if obj.status.isAlive:
                            PRO.append(obj.status)
                    else:
                        player_state[5] = obj.status.hp
                        player_state[6] = obj.status.moveCD
                        player_state[7] = obj.status.moveCDLeft
                        player_state[8] = obj.status.rebornTimeLeft
                        player_state[9] = obj.status.godTimeLeft
                        state[2][block.x][-block.y] = -1
                        if obj.status.isAlive:
                            CON.append(obj.status)

                elif obj.type == ObjType.Item:
                    state[3][block.x][-block.y] += obj.status.buffType + 1

                elif obj.type == ObjType.SlaveWeapon:
                    if obj.status.playerID == playerId:
                        state[4][block.x][-block.y] = 1
                    else:
                        state[4][block.x][-block.y] = -1

        # elif len(block.objs) == 0 and block.color != color:
        #     state[0][block.x][-block.y] = -1.0
        # '''3:无视野判断'''
        if not block.valid:
            state[0][block.x][-block.y] = 0.0

    '''两个角色防止混淆'''
    if len(PRO) > 1:
        if PRO[0].characterID > PRO[1].characterID:
            PRO[0], PRO[1] = PRO[1], PRO[0]

    return torch.concat([state.view(-1), player_state]), slaveryCD, (CON, PRO)


count = 0
count1 = 0
count2 = 0
count3 = 0

unit_dir = torch.tensor([[-1, -1], [-1, 0], [0, 1], [1, 1], [1, 0], [0, -1]])

def Get_rand(pos = (7, 7)):
    print('random!!!')
    if pos[0] == 0:
        return torch.randint(3, 5, (1,)).item()
    if pos[1] == 0:
        return torch.randint(2, 4, (1,)).item()
    return torch.randint(0, 6, (1,)).item()


def Distance(x, y):
    return torch.dist(x + 0.0, y + 0.0, p = 2)
    # return torch.dist(x + 0.0, y + 0.0, p = 1)


def Select_dir(pos, aim_pos):
    global unit_dir
    distances = torch.tensor(list(map(partial(Distance, aim_pos), pos + unit_dir)))
    optimal_dir = torch.argmin(distances)
    return optimal_dir.item()


def Nearest(pos, aims):
    distances = torch.tensor(list(map(partial(Distance, pos), aims)))
    nearest = torch.argmin(distances)
    return aims[nearest]


def Chase_filter(pos, aims, go_Vacant: bool = False):
    filter1 = aims[[i for i in range(aims.shape[0]) if Distance(aims[i], pos) > int(go_Vacant)]]
    filter2 = filter1[[i for i in range(filter1.shape[0]) if aims[i, 0] < 21 and aims[i, 1] < 21]]
    return filter2

'''根据当前位置和方向判断是否attack'''
# def Attack(pos, d):
#     global unit_dir
#     next_pos = torch.stack([pos[0] + unit_dir[d[0]], pos[1] + unit_dir[d[1]]])
#     attack_center = torch.stack([next_pos[0] + unit_dir[d[0]], next_pos[1] + unit_dir[d[1]]])
    


def state2actions(state, characters):
    global count, count1, count2, count3

    obs = state[:-10].view((5, 24, 24))
    CON, PRO = characters[0], characters[1]
    pos = [None, None]
    if len(PRO) >= 1:
        pos[0] = torch.tensor([PRO[0].x, -PRO[0].y])
    if len(PRO) == 2:
        pos[1] = torch.tensor([PRO[1].x, -PRO[1].y])
    
    aim_pos = [None, None]
    if len(CON) >= 1:
        aim_pos[0] = torch.tensor([CON[0].x, -CON[0].y])
    if len(CON) == 2:
        aim_pos[1] = torch.tensor([CON[1].x, -CON[1].y])

    if len(PRO) + len(CON) >= 3:
        if len(PRO) == 1:
            if Distance(pos[0], aim_pos[0]) > Distance(pos[0], aim_pos[1]):
                tmp = aim_pos[0]
                aim_pos[0] = aim_pos[1]
                aim_pos[1] = tmp
                CON[0], CON[1] = CON[1], CON[0]
        else:
            if Distance(pos[0], aim_pos[0]) > Distance(pos[1], aim_pos[0]):
                tmp = pos[0]
                pos[0] = pos[1]
                pos[1] = tmp
                PRO[0], PRO[1] = PRO[1], PRO[0]


    d = [-1, -1]
    for i in range(len(PRO)):
        # if aim_pos[i] is not None:
        #     d[i] = Select_dir(pos[i], aim_pos[i])
        #     continue

        vacant_indices = Chase_filter(pos[i], torch.nonzero(obs[0] == -1), True)
        speed_indices = Chase_filter(pos[i], torch.nonzero(obs[3] == 2))
        hp_indices = Chase_filter(pos[i], torch.nonzero(obs[3] == 3))
        
        if PRO[i].hp <= 50:
            if hp_indices.shape[0] != 0:
                hp_pos = Nearest(pos[i], hp_indices)
                d[i] = Select_dir(pos[i], hp_pos)
                continue
            if speed_indices.shape[0] != 0 and PRO[i].moveCD >= 2:
                speed_pos = Nearest(pos[i], speed_indices)
                d[i] = Select_dir(pos[i], speed_pos)
                continue
            elif vacant_indices.shape[0] != 0 and d[i] == -1:
                vacant_pos = Nearest(pos[i], vacant_indices)
                d[i] = Select_dir(pos[i], vacant_pos)
                continue
            elif aim_pos[i] is not None and CON[i].isAlive and not CON[i].isGod:
                d[i] = Select_dir(pos[i], aim_pos[i])
                continue
        else:
            if aim_pos[i] is not None and CON[i].isAlive and not CON[i].isGod:
                d[i] = Select_dir(pos[i], aim_pos[i])
                continue

            if speed_indices.shape[0] != 0 and PRO[i].moveCD >= 2:
                speed_pos = Nearest(pos[i], speed_indices)
                d[i] = Select_dir(pos[i], speed_pos)
                continue
            if hp_indices.shape[0] != 0 and PRO[i].hp < 100 and d[i] == -1:
                hp_pos = Nearest(pos[i], hp_indices)
                d[i] = Select_dir(pos[i], hp_pos)
                continue
            if vacant_indices.shape[0] != 0 and d[i] == -1:
                vacant_pos = Nearest(pos[i], vacant_indices)
                d[i] = Select_dir(pos[i], vacant_pos)
                continue
            
        if d[i] == -1:
            d[i] = Get_rand(pos[i])

    if len(PRO) == 2 and Distance(pos[0], pos[1]) < 3:
        d[1] = (Select_dir(pos[1], pos[0]) + 3) % 6


    return tuple(d)


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


def state2actionReq(state, characters):
    direction = state2actions(state, characters)
    PRO = characters[1]

    actions = []

    if len(PRO) >= 1 and direction[0] != -1:
        actions = [ActionReq(PRO[0].characterID, *int2action[direction[0]])]
        if PRO[0].slaveWeapon.attackCDLeft == 0 or PRO[0].masterWeapon.attackCDLeft == 0:
            actions.append(ActionReq(PRO[0].characterID, ActionType.UnSneaky, EmptyActionParam()))
        actions.append(ActionReq(PRO[0].characterID, *int2action[7]))
        actions.append(ActionReq(PRO[0].characterID, *int2action[8]))
        actions.append(ActionReq(PRO[0].characterID, *int2action[-1]))
        actions.append(ActionReq(PRO[0].characterID, ActionType.Sneaky, EmptyActionParam()))
        # look back
        # actions.append(ActionReq(PRO[0].characterID, *int2action[(direction[0]+3)%6]))
    if len(PRO) == 2 and direction[1] != -1:
        actions.append(ActionReq(PRO[1].characterID, *int2action[direction[1]]))
        if PRO[1].slaveWeapon.attackCDLeft == 0 or PRO[1].masterWeapon.attackCDLeft == 0:
            actions.append(ActionReq(PRO[1].characterID, ActionType.UnSneaky, EmptyActionParam()))    
        actions.append(ActionReq(PRO[1].characterID, *int2action[7]))
        actions.append(ActionReq(PRO[1].characterID, *int2action[8]))
        actions.append(ActionReq(PRO[1].characterID, *int2action[-1]))
        actions.append(ActionReq(PRO[1].characterID, ActionType.Sneaky, EmptyActionParam()))
        # look back
        # actions.append(ActionReq(PRO[1].characterID, *int2action[(direction[1]+3)%6]))

        
    return actions


# def routeTest(PRO):
#     # CON, PRO = characters[0], characters[1]
#     dir = certainRoute(PRO)

#     actions = [ActionReq(PRO[0].characterID, *int2action[dir[0]])]
#     actions.append(ActionReq(PRO[0].characterID, *int2action[8 if PRO[0].slaveWeapon.attackCDLeft == 0 else 7]))
#     actions.append(ActionReq(PRO[0].characterID, *int2action[-1]))

#     actions.append(ActionReq(PRO[1].characterID, *int2action[dir[1]]))
#     actions.append(ActionReq(PRO[1].characterID, *int2action[8 if PRO[1].slaveWeapon.attackCDLeft == 0 else 7]))
#     actions.append(ActionReq(PRO[1].characterID, *int2action[-1]))
#     return actions


def refreshUI(ui: UI, packet: PacketResp):
    """Refresh the UI according to the response."""
    data = packet.data
    if packet.type == PacketType.ActionResp:
        ui.playerID = data.playerID
        ui.color = data.color
        ui.characters = data.characters
        ui.score = data.score
        ui.kill = data.kill
        ui.frame = data.frame

        for block in data.map.blocks:
            if len(block.objs):
                ui.block = {
                    "x": block.x,
                    "y": block.y,
                    "color": block.color,
                    "valid": block.valid,
                    "frame": block.frame,
                    "obj": block.objs[-1].type,
                    "data": block.objs[-1].status,
                }
            else:
                ui.block = {
                    "x": block.x,
                    "y": block.y,
                    "color": block.color,
                    "valid": block.valid,
                    "frame": block.frame,
                    "obj": ObjType.Null,
                }
    subprocess.run(["clear"])
    ui.display()


def main():
    ui = UI()

    '''🤡🥵🤡🥵🤡🥵🤡🥵🤡🥵🤡🥵🤡🥵🤡🥵🤡🥵🤡🥵🤡🥵🤡'''
    with Client() as client:
        client.connect()
        init_req = InitReq(MasterWeaponType.Durian, SlaveWeaponType.Cactus)
        # init_req1 = InitReq(MasterWeaponType.PolyWatermelon, SlaveWeaponType.Kiwi)
        init_packet = PacketReq(PacketType.InitReq, [init_req] * 2)
        client.send(init_packet)
        resp = client.recv()
        # print(resp.data.characters)

        refreshUI(ui, resp)

        '''3:我方的characterId'''
        characterIds = [resp.data.characters[0].characterID, resp.data.characters[0].characterID]

        while resp.type != PacketType.GameOver:
            next_state, slaveryCD, characters = actionResp2info(resp)
            state = next_state

            '''4:need to be modify'''
            actionReq = state2actionReq(state, characters)

            actionPacket = PacketReq(PacketType.ActionReq, actionReq)
            client.send(actionPacket)
            resp = client.recv()

            refreshUI(ui, resp)


if __name__ == '__main__':
    from main import Client

    main()
