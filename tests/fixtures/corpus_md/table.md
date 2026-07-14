# 环境矩阵

各环境的资源配额与访问入口如下表：

| 环境 | 数据库 | 副本数 | 入口域名 |
| --- | --- | --- | --- |
| dev | pg-dev（共享） | 1 | dev.mall.internal |
| staging | pg-stg（独立） | 2 | stg.mall.internal |
| prod | pg-prod（独立，双可用区） | 4 | mall.example.com |

表格之后的段落：staging 与 prod 的配置差异仅允许出现在副本数与域名两项。
