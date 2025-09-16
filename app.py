#!/usr/bin/env python3
import aws_cdk as cdk
from image_name_cap_stack_neptune import ImageNameCapStackNeptune as ImageNameCapStack

app = cdk.App()
ImageNameCapStack(app, "ImageNameCapStack", env=cdk.Environment(region="us-west-2"))
app.synth()