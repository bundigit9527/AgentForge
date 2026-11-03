"""Version-one numeric CSV exchanged with the registered Computer service."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

EXPORT_TOOL = "export_number_summary_csv"


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    count: int = Field(ge=1, le=512)
    minimum: int
    maximum: int
    total: int
    mean: float

    def csv_text(self):
        return (
            "count,minimum,maximum,total,mean\r\n"
            f"{self.count},{self.minimum},{self.maximum},{self.total},{self.mean}\r\n"
        )


class CsvSummary(Summary):
    csv: str = Field(max_length=512)

    @model_validator(mode="after")
    def validate_csv(self):
        if self.csv != self.csv_text():
            raise ValueError("CSV must match the numeric summary")
        return self


def exports_csv(tool):
    return tool.server_id == "computer" and tool.tool_name == EXPORT_TOOL and tool.version == 1
