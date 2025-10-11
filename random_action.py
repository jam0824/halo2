from motor_controller import MotorController
import time

class RandomAction:
    def __init__(self, motor_controller: MotorController, config: dict):
        self.motor_controller = motor_controller
        self.config = config

    def reset_timer(self):
        self.start_timer = time.time()
        print("reset_timer")

    def random_action(self, random_action_time: float):
        print(time.time() - self.start_timer)
        if time.time() - self.start_timer > random_action_time:
            print("random_action")
            if self.config["motor"]["use_motor"]:
                self.motor_controller.motor_pan_kyoro_kyoro(3, 2)
            self.reset_timer()