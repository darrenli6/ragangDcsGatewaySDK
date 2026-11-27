
帮我写一个sdk ,你考虑业绩水准的标准 写好测试用例 调用示例
叫ragany_dcs_gateway_sdk
工业 HTTP 采集、发酵 DCS 项目

**httpx（优先推荐，工业采集）**
- 支持同步 + async，连接池、超时、重试、长轮询都稳，工业场景断线多很合适
- 类型友好，配合 Pydantic 做点位模型（pH、温度、液位等），IDE 自动提示

├── __init__.py               # 对外暴露入口类
├── client.py                # 核心HttpClient（http请求、重试、超时、鉴权）
├── models.py                # Pydantic模型：PhPoint、TempPoint、BatchData（工艺点位）
├── exceptions.py            # 自定义异常：网关离线、点位不存在、校验失败 
├── config.py                # 配置：网关地址、采集周期、token
├── utils.py                 # 工具：时间格式化、点位映射、报文校验
└── examples/
    └── demo.py              # 使用示例：读取pH、温度


调用格式 http://192.168.1.88/soap/函数名称
函数名称 功能说明 API 版本支持
BrowseTag 获取点位结构 1.0
ReadTag 读取点位的值

2.3.1 BrowseTag
函数名称 BrowseTag
请求 url http://192.168.1.88/soap/BrowseTag
功能说明 获取网关所有的点位结构信息
请求参数
说明
node 参数为空，获取所有点位结构信息
node 参数不为空，需要填入指定路径信息（例如 Driver.Channel）,获取指定点位结
构信息
请求参数 {
"node": ""
}
响应内容 {
"drivers": [],
"name": "VarDevice",
"tags": []
}
 
响应参数
说明 drivers 驱动结构 channels 通道结构 devices 设备结构 groups 组结构
tags 点位结构 name 名称
d 描述
n 点名
t 数据类型 1~8(1-Boolean 2-Word 3-Short 4-Dword 5-Long 6-Float 7-Double 8-String)
u 单位（95-无单位）
v 当前值
w 读写标志(0 只读，1 读写

2.3.2 ReadTag
函数名称 ReadTag
请求 url http://192.168.1.88/soap/ReadTag
功能说明 读取点位的当前值，支持批量读。
参数说明 name 为需要读取点位的全路径名称(驱动名称.通道名称.设备名称.点位名
称)，如果有设备下有分组，那么还需要加上组的名称。
输入参数 {
"data": [
{
"name": "s.CP201.D122.qq1"
},
{
"name": "s.CP201.D122.qq2"
}
]
}
响应内容 {
"data": [
{
"name": "s.CP201.D122.qq1",
"value": "9"
},
{
"name": "s.CP201.D122.qq2",
"value": "0"
}
]
}