from pydantic import BaseModel, Field, ConfigDict


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", description="agent名字，例如「林志安」")
    age: str = Field(default="", description="agent年齡，例如「35 歲」")
    occupation: str = Field(default="", description="agent職業，例如「公司行政主管」")
    wage: str = Field(default="", description="agent個人收入，例如「新台幣 6 萬元每月」")
    household_income: str = Field(default="", description="agent家戶收入，例如「新台幣 18 萬元每月」")
    vehicle_ownership: str = Field(default="", description="agent交通工具持有狀況，例如「無私家車，擁有一輛共享單車」")
    residential_location: str = Field(default="", description="agent居住地點，例如「台南市西區南門路附近的公寓」")


class Traits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attitudes: list[str] = Field(
        default_factory=list,
        min_length=1,
        max_length=1,
        description="agent對球賽交通、場館服務、壅塞或政策的態度，例如「對於交通擁擠的情況感到不耐煩，但願意為了觀看比賽而調整行程」"
    )
    habits: list[str] = Field(
        default_factory=list,
        min_length=1,
        max_length=1,
        description="agent平常或比賽日時的交通與活動習慣，例如「偏好搭乘台鐵或台南市區公車前往」"
    )
    decision_making_tendencies: list[str] = Field(
        default_factory=list,
        min_length=1,
        max_length=1,
        description="agent在選擇路線、交通工具、停車或改道時的決策傾向，例如「遇到交通堵塞時會主動尋找附近的即時交通資訊並決定改道」"
    )
    economic_preferences_and_tradeoffs: list[str] = Field(
        default_factory=list,
        min_length=1,
        max_length=1,
        description="agent在時間、費用、便利性、停車與等待時間之間的取捨，例如「願意為減少排隊時間支付額外的停車費或公車高峰加價票」"
    )


class Memory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    short_term_memory: list[str] = Field(
        default_factory=list,
        min_length=0,
        max_length=0,
        description="agent短期記憶，目前必須固定輸出空 array，例如 []"
    )
    long_term_memory: list[str] = Field(
        default_factory=list,
        min_length=0,
        max_length=0,
        description="agent長期記憶，目前必須固定輸出空 array，例如 []"
    )


class AgentSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: Identity = Field(default_factory=Identity, description="agent的基本身分資訊")
    traits: Traits = Field(default_factory=Traits, description="agent的態度、習慣、決策傾向與經濟取捨")
    memory: Memory = Field(default_factory=Memory, description="agent的記憶資料，目前短期與長期記憶都必須為空 array")


class AgentProfileSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agents: list[AgentSchema] = Field(
        default_factory=list,
        min_length=1,
        description="交通模擬用 agent profile 清單，包含一個或多個 agent object"
    )
