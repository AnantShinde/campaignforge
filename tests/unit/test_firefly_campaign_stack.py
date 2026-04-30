import aws_cdk as core
import aws_cdk.assertions as assertions

from firefly_campaign.firefly_campaign_stack import FireflyCampaignStack

# example tests. To run these tests, uncomment this file along with the example
# resource in firefly_campaign/firefly_campaign_stack.py
def test_sqs_queue_created():
    app = core.App()
    stack = FireflyCampaignStack(app, "firefly-campaign")
    template = assertions.Template.from_stack(stack)

#     template.has_resource_properties("AWS::SQS::Queue", {
#         "VisibilityTimeout": 300
#     })
