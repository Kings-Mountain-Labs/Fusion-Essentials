#  Copyright 2023 by Ian Rist

import time

class Timer:
    def __init__(self):
        self.events = {}
        self.current_event = None

    def mark(self, event_name):
        if ':' in event_name:
            main_event, sub_event = event_name.split(':', 1)
            if main_event not in self.events:
                self.events[main_event] = {"time": 0.0, "subevents": {}}
            if sub_event not in self.events[main_event]["subevents"]:
                self.events[main_event]["subevents"][sub_event] = {"time": 0.0}
            event_to_track = self.events[main_event]["subevents"]
        else:
            if event_name not in self.events:
                self.events[event_name] = {"time": 0.0, "subevents": {}}
            event_to_track = self.events[event_name]

        if self.current_event:
            current_time = time.time()
            elapsed = current_time - self.current_event["start"]
            if ':' in self.current_event["name"]:
                main_event, sub_event = self.current_event["name"].split(':', 1)
                self.events[main_event]["subevents"][sub_event]["time"] += elapsed
            else:
                self.events[self.current_event["name"]]["time"] += elapsed

        self.current_event = {
            "start": time.time(),
            "name": event_name,
            "ref": event_to_track
        }

    def finish(self):
        if self.current_event:
            current_time = time.time()
            elapsed = current_time - self.current_event["start"]
            if ':' in self.current_event["name"]:
                main_event, sub_event = self.current_event["name"].split(':', 1)
                self.events[main_event]["subevents"][sub_event]["time"] += elapsed
            else:
                self.events[self.current_event["name"]]["time"] += elapsed
            self.current_event = None

        # add up all the time for the main events
        for event, details in self.events.items():
            details["time_nonsub"] = details["time"]
            if "subevents" in details:
                for subevent, subevent_details in details["subevents"].items():
                    details["time"] += subevent_details["time"]
        good_events = self.events
        self.events = {}
        return good_events
    
def format_timer(timing_dict):
    output = []

    for event, details in timing_dict.items():
        output.append(f"Event: {event}")
        output.append(f"\tTime: {details['time']:.5f} seconds")
        if "time_nonsub" in details:
            output.append(f"\tTime (non-subevents): {details['time_nonsub']:.5f} seconds")

        subevents = details.get('subevents', {})
        # sort subevents by their key
        subevents = dict(sorted(subevents.items(), key=lambda item: item[0]))
        for subevent, subevent_item in subevents.items():
            output.append(f"\tSubevent: {subevent}\t\tTime: {subevent_item['time']:.5f} seconds")

    return '\n'.join(output)