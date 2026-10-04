"""模拟模式下 Claude 返回的示例分镜（也可作为手写分镜的格式参考）。"""

MOCK_STORYBOARD = {
    "title": "最后一班列车",
    "logline": "深夜末班地铁上，一个疲惫的女孩遇见了一位知道她名字的老人。",
    "style": "cinematic, moody night lighting, teal and amber color grade, 35mm film grain, shallow depth of field",
    "characters": [
        {
            "id": "lin",
            "name": "林夏",
            "description": "Chinese woman, 26, shoulder-length black hair, tired eyes, beige trench coat over a grey hoodie, canvas tote bag",
        },
        {
            "id": "oldman",
            "name": "老人",
            "description": "Chinese man, about 70, neatly combed white hair, round wire glasses, dark navy wool coat, holding an old paper ticket",
        },
    ],
    "shots": [
        {
            "id": 1,
            "duration": 5,
            "characters": ["lin"],
            "shot_type": "wide",
            "keyframe_prompt": "Wide shot of an empty subway platform at midnight, Lin standing alone near the platform edge under flickering fluorescent lights, train headlights approaching from the tunnel",
            "motion_prompt": "Slow push-in toward Lin as the train's headlights sweep across her face, her coat moves in the gust of wind",
            "dialogue": "",
        },
        {
            "id": 2,
            "duration": 6,
            "characters": ["lin", "oldman"],
            "shot_type": "medium",
            "keyframe_prompt": "Inside a nearly empty subway car at night, Lin sits on the bench looking at her phone, the old man sits across from her smiling gently",
            "motion_prompt": "Static camera, the car sways slightly, the old man leans forward and speaks softly",
            "dialogue": "老人：林夏，你今天又加班到这么晚啊。",
        },
        {
            "id": 3,
            "duration": 5,
            "characters": ["lin"],
            "shot_type": "close-up",
            "keyframe_prompt": "Close-up of Lin's face, startled, reflections of tunnel lights streaking across the window behind her",
            "motion_prompt": "Lin slowly lowers her phone and looks up, eyes widening, subtle handheld camera shake",
            "dialogue": "林夏：……您怎么知道我的名字？",
        },
    ],
}
